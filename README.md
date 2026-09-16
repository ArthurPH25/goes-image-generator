# GOES Image Generator

Gerador de imagens estáticas e vídeos a partir dos satélites GOES-16/GOES-19 (bandas ABI 1-16 e GLM), com foco na América do Sul. Baixa os NetCDF direto do bucket público da NOAA na AWS, recorta a região de interesse, aplica as paletas definidas em `colors.ini` e renderiza com matplotlib/cartopy, seja para uma imagem única ou para um vídeo compilado com FFmpeg.

Toda a configuração de execução (canal, período, geometria do mapa, cores, performance) fica em `config.ini`/`colors.ini`, os dois comentados linha a linha. Este README cobre o que os `.ini` não cobrem: como o pipeline funciona por dentro e o que esperar quando as coisas dão errado.

## Arquitetura e fluxo do pipeline

### 1. Validação e assinatura de render (`index.py`, nível de módulo)

O parsing e a validação de `config.ini`/`colors.ini` rodam no nível de módulo, fora de qualquer função, antes do `if __name__ == "__main__"`. Qualquer erro de config aborta (`exit_fatal`) antes de tocar em rede ou subir um worker.

Nessa mesma etapa é calculada a `render_signature`: um SHA1 (12 chars) de tudo que afeta a aparência do frame, isto é, canal, `[MAP]` (projeção, coordenadas, tamanho da bolinha do raio), `[STYLE]` inteiro, `dpi`, banda de fundo do GLM e as seções de paleta relevantes ao canal ativo. Data e horário **não** entram na assinatura, de propósito.

Essa assinatura decide a pasta de trabalho do modo vídeo (`satelite_temp_images/<assinatura>/`):
- Mudar só o período do vídeo (`start_time`/`end_time`) mantém a mesma assinatura, então frames já renderizados são reaproveitados e só os timestamps novos são processados.
- Mudar qualquer coisa visual (cor, projeção, canal, dpi...) gera assinatura nova, então a pasta é criada do zero.

No modo imagem única (`I`) essa lógica nem entra em jogo: a saída vai direto pra `satelite_images/`, sem cache entre execuções.

### 2. Busca no S3 (síncrona, fora do pool)

A NOAA não expõe nome de arquivo previsível, então o timestamp exato é resolvido listando a pasta da hora (`s3fs.ls`) e filtrando por prefixo/canal. Essa busca roda sequencial e sincronamente no processo principal, um `ls` por timestamp-alvo (e mais um `ls` na pasta ABI, se `glm_background_band` estiver setado). Não há paralelismo aqui de propósito: listar é rápido, e paralelizar isso só aumentaria a chance de throttle da NOAA logo no início da execução.

### 3. Renderização (`ProcessPoolExecutor`, paralelismo misto)

Cada timestamp resolvido vira uma tarefa distribuída entre `num_workers` processos. Dentro de cada worker o fluxo é sequencial:

1. Download do NetCDF principal via `S3_downloader.download_batch`, que sobe seu próprio `asyncio.run()` com um semáforo de `max_concurrent_downloads`.
2. Render com matplotlib/cartopy (`goes_bands.py` ou `GLM.py`), que é CPU-bound e por isso os workers são processos, não threads.

Pro canal GLM, o worker ainda dispara um **segundo** `asyncio.run()` (dentro de `GLM.generate_image` → `_fetch_flash_history`) pra buscar o histórico de raios (`glm_history_lookback_steps` passos de 20s pra trás), com semáforo próprio (`glm_history_max_concurrent_downloads`). Os dois `asyncio.run()` do worker (download principal e histórico) rodam em sequência, não simultaneamente.

`goes_bands.py` e `GLM.py` só extraem a banda/os raios e desenham; projeção geoestacionária↔lat/lon, geometria da figura, título, marca d'água e os locks de arquivo ficam centralizados em `utils.py`.

### 4. Montagem do vídeo

Depois que todos os workers terminam, o vídeo é montado fora do pool (processo principal), lendo os PNGs da pasta de assinatura, validando cada um e entregando pro FFmpeg via `imageio`.

## Comportamentos e casos de borda

**Cache do fundo ABI no GLM: por bloco de 10 min, com lock, só existe em vídeo.** Quando `glm_background_band` está configurado, todo frame GLM cujo timestamp caia no mesmo bloco de 10 minutos aponta pro mesmo `.npz` (já cortado e com colormap resolvido, não o NetCDF cru). Só o primeiro worker a chegar baixa e processa; os demais leem o cache. Isso é protegido por lock de arquivo com timeout de 300s (5 min). Se o lock estourar esse tempo, situação típica com `num_workers` alto disputando o mesmo fundo, o worker desiste de esperar e **renderiza aquele frame sem fundo**, com aviso no console; não é fatal e não trava os outros frames. Em imagem única (`I`) esse cache/lock nem existe: o fundo é baixado e processado direto, sem ninguém pra disputar.

**Histórico do GLM nunca falha por causa de lock.** Cada arquivo de histórico de raios é baixado pra um temporário atômico por processo (sufixo `.part_<pid>`) e só depois vira o arquivo de cache via `os.replace()`. Se o lock desse arquivo específico não vier a tempo, o worker não desiste: baixa mesmo assim (com aviso) pro seu próprio temporário e faz o replace no final. No pior caso o download fica duplicado; nunca dá corrupção ou raio faltando no histórico.

**Arquivo ausente na NOAA: fatal em imagem única, aviso em vídeo, mas não por um `if` explícito pra isso.** É consequência de `image_date`/`image_time` gerarem só 1 timestamp-alvo: se ele não for encontrado no S3, a lista de tarefas fica vazia e a execução aborta porque não sobrou nada pra renderizar. No vídeo há vários timestamps-alvo; o que faltar é só logado como aviso e não entra na lista de tarefas, e o resto segue normal, com o vídeo final saindo sem aquele frame.

**Uma instância por pasta.** `satelite_temp_downloads/` e o cache de NetCDF do GLM são compartilhados entre todos os workers da execução e são **apagados por inteiro ao final**, com sucesso ou não. Por isso a execução trava com erro fatal se já existir `instance.lock` ativo na pasta: duas instâncias juntas destruiriam os temporários uma da outra no meio do processamento. O lock expira sozinho depois de 24h (proteção contra lock órfão de uma execução anterior que travou sem limpar).

**PNG corrompido é validado duas vezes, nunca chega no FFmpeg.** Todo PNG já existente na pasta de assinatura (modo vídeo) é validado com `PIL.Image.verify()` antes de ser considerado "pronto". Isso cobre o caso de Ctrl+C ou queda de energia no meio da gravação de um frame, que deixa um PNG truncado em disco; se inválido, é apagado e re-renderizado. A mesma validação roda de novo na montagem final: frames corrompidos remanescentes são descartados nessa etapa, em Python, e o FFmpeg nunca chega a ver esses arquivos. Frames com dimensão diferente da do primeiro do lote são redimensionados (LANCZOS) antes de entrar no vídeo.

## Instalação e pré-requisitos

- **Cartopy exige GEOS/PROJ no sistema.** O caminho de menor atrito é Conda (`conda install -c conda-forge cartopy`): o conda-forge empacota os binários prontos, então o comando é o mesmo nos três SOs. Via `pip` puro cada SO vira uma dor de cabeça diferente pra resolver essas libs nativas.
- **FFmpeg não precisa estar no PATH.** `imageio-ffmpeg` (já no `requirements.txt`) baixa e gerencia seu próprio binário automaticamente; não tem instalação de FFmpeg separada pra fazer.
- **Headless já vem pronto.** `matplotlib.use("Agg")` é forçado no topo de `utils.py`, `goes_bands.py` e `GLM.py`, então rodar em servidor/SSH sem display não exige nenhuma variável de ambiente extra.

## Estrutura de pastas de saída

| Pasta | Conteúdo | Sobrevive entre execuções? |
|---|---|---|
| `satelite_images/` | PNGs do modo imagem única (`I`) | Sim (nome incrementa automaticamente pra não sobrescrever) |
| `satelite_videos/` | MP4s finais do modo vídeo (`V`) | Sim (mesmo esquema de incremento) |
| `satelite_temp_images/<assinatura>/` | Frames PNG do modo vídeo, uma pasta por `render_signature` | Só se `delete_temp_images = False` |
| `satelite_temp_downloads/` | NetCDF baixados na execução atual + cache `.npz` do fundo ABI do GLM | Não, é apagado por inteiro ao final, independente de `delete_temp_images` |
| `satelite_temp_downloads/glm_nc_cache/` | NetCDF brutos do GLM (arquivo principal + histórico), reaproveitados entre frames da mesma execução | Não, mesmo apagamento incondicional |
| `satelite_temp_downloads/instance.lock` | Trava de execução única por pasta | Não (removido ao final; expira sozinho em 24h se sobrar de uma queda) |

## Licença

Este projeto está sob a licença [MIT](LICENSE).