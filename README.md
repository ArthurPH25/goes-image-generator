# GOES Image Generator

Gerador de imagens e vídeos a partir de dados brutos dos satélites GOES-16 e GOES-19 (NOAA), otimizado para a **América do Sul**. Renderiza as 16 bandas espectrais do ABI e o mapeador de raios GLM, com colormaps, projeção cartográfica e estilização customizáveis via `.ini`.

O script baixa os NetCDF direto do bucket público S3 da NOAA (`noaa-goes16` / `noaa-goes19`), recorta a região escolhida, aplica a paleta de cores correspondente e renderiza com `matplotlib` + `cartopy`. A saída é um PNG avulso ou um MP4 (montado via `ffmpeg`/`imageio` a partir da sequência de frames).

---

## Arquitetura

Orquestração em `index.py`:

1. **Validação** — `config.ini` inteiro (`[GENERAL]`, `[IMAGE_TIME]`/`[VIDEO_TIME]`, `[MAP]` e `[STYLE]`) é parseado e validado antes de qualquer busca ou download, inclusive para calcular a assinatura de cache do vídeo. Chave errada ou faltando derruba o script na hora, sem gastar uma única requisição no S3. A única validação que sobra pra dentro do worker é a paleta de cores em `colors.ini` (ex: chave não-inteira em `[PALETTE_BAND_XX]`) — um erro nela só aparece depois que a busca no S3 já rodou, porque o script só lê a paleta de fato ao renderizar o primeiro frame.
2. **Busca no S3** — localização dos arquivos no bucket. Troca automaticamente de satélite: GOES-16 até 07/04/2025, GOES-19 depois disso.
3. **Download assíncrono** — `S3_downloader.py` baixa via `asyncio` + `s3fs`, com retry exponencial, checagem de integridade e tratamento de 404.
4. **Renderização**:
   - `goes_bands.py` — as 16 bandas ABI (refletância em escala de cinza ou paleta térmica customizada), com recorte de mapa e overlays.
   - `GLM.py` — densidade de raios GLM, com rastro histórico (idade codificada por cor e tamanho de marcador) e overlay opcional sobre fundo ABI (com cache compartilhado entre frames).
5. **Paralelização** — frames processados via `ProcessPoolExecutor` (CPU); downloads de cada frame rodam concorrentemente via `asyncio` (I/O).
6. **Vídeo** — se `generation_type = V`, `imageio`/`ffmpeg` costura os frames em `.mp4` com controle de CRF, preset e escala.
7. **Encerramento** — o script imprime tempo de execução por etapa e um relatório consolidado de avisos (⚠️) e erros (❌) não fatais, incluindo os que rodaram nos workers. Erros fatais (☠️) não entram nesse relatório porque já interrompem o script na hora.

`utils.py` concentra o que é compartilhado entre os módulos: validação de config, projeção geoestacionária, recorte de área, marca d'água, título e o file lock que evita dois workers renderizando o mesmo fundo GLM ao mesmo tempo.

---

## Foco regional: América do Sul

- **Satélite de referência**: GOES-19 (75°W) cobre o continente desde abril de 2025; datas anteriores usam GOES-16 automaticamente.
- **Recorte**: `target_coordinates` (Oeste, Leste, Sul, Norte) recorta só a área de interesse — menos CPU/RAM e arquivos de saída mais leves.
- **Fuso horário**: tudo em UTC — busca no bucket, timestamps, títulos e nomes de arquivo.
- **Cadência**: segue o Full Disk real — 10 minutos para ABI, 20 segundos para GLM.

---

## Configuração

Dois arquivos `.ini` na raiz do projeto:

- **`config.ini`** — canal, período, workers, projeção, recorte, GLM e renderização de vídeo.
- **`colors.ini`** — paletas hexadecimais por banda térmica e por idade de raio GLM.

Não há valores padrão implícitos: chave ausente ou fora do formato esperado interrompe o script. Isso vale para as chaves realmente usadas no modo ativo — `[IMAGE_TIME]` só é lida se `generation_type = I`, `[VIDEO_TIME]` só se `generation_type = V` (a seção do modo inativo pode ficar com qualquer coisa, ela nem é tocada).

### Pontos de atenção (comportamento real, não intuitivo)

1. **Horário ABI vs. GLM** — ABI usa `HH:MM`; GLM usa `HH:MM:SS`. Trocar `channel` exige ajustar o formato do horário correspondente, senão a validação quebra.
2. **Ordem das coordenadas** — o script confere se `target_coordinates` tem 4 números, mas não checa se Oeste < Leste e Sul < Norte. Inverter os valores passa na validação e sai com o mapa cortado errado.
3. **Timestamp ausente no bucket**: no modo vídeo (`V`), o frame ausente é pulado com aviso no console e o vídeo segue sem ele. No modo imagem única (`I`), se o instante pedido não existir, o script encerra com **erro fatal** — não há frame de fallback nesse modo.
4. **Modo imagem única sobrescreve sem perguntar** — diferente do modo vídeo (que reaproveita frame já existente no cache), o modo `I` sempre re-renderiza e sobrescreve o PNG de saída, mesmo se já existir um com o mesmo nome.
5. **`fps` do `config.ini` só vale para vídeo** — no modo `I` a seção `[VIDEO_TIME]` inteira nem é lida, então o `fps` configurado não tem efeito nenhum nesse modo (não aparece em lugar algum, nem como metadado).
6. **`glm_flash_age = False` desliga tudo** — só mostra os raios do segundo exato do frame. O rastro histórico morre, a legenda some e `glm_history_lookback_steps` fica sem efeito.
7. **Sufixo de cache `_CXX` no GLM** — preencher `glm_background_band` adiciona a banda no nome do frame (`_C13.png`). Banda inválida (fora de 1–16) só estoura erro **depois** que o arquivo GLM principal já foi baixado.
8. **Lag do fundo GLM (não é bug)** — ABI atualiza a cada 10 min, GLM a cada 20s. O fundo sempre arredonda **para trás** pro múltiplo de 10 anterior — um raio das 19:19:40 usa o fundo ABI das 19:10 (lag de até 9min40s). É limitação física do satélite, não do script.
9. **Lock de fundo em vídeos GLM** — com `num_workers` alto (12+) e fundo ABI habilitado, um worker pode esperar mais de 5 minutos pela renderização do fundo por outro worker. Se estourar, o frame sai sem fundo com aviso amarelo. Se acontecer com frequência, abaixe `num_workers`. O download do *histórico* de raios (não o fundo ABI) tem uma diferença sutil aqui: se o lock dele estourar, o script tenta baixar mesmo assim (é seguro — usa arquivo temporário por processo e troca atômica), então nunca fica sem histórico por causa de lock, só sem fundo ABI.
10. **Isolamento de `satelite_temp_images/<assinatura>/`** — cada combinação de parâmetros que afeta o pixel final (canal, `target_coordinates`, `projection`, `dpi`, tudo em `[STYLE]`, paletas de cor envolvidas) gera um subdiretório próprio, calculado no início da execução. Rodar de novo com os mesmos parâmetros reaproveita o cache (útil pra retomar vídeo interrompido); mudar qualquer parâmetro visual cai num subdiretório novo. Com `delete_temp_images = True`, só o subdiretório **desta execução** é apagado ao final. Pra limpar cache acumulado de execuções antigas, apague `satelite_temp_images/` inteira — ela é recriada na próxima execução.
11. **Proporção do título** — a barra do título tem altura fixa em polegadas (quando `clean_mode = False`). Se a imagem parecer "engolida" pelo título, aumente `figure_height`.
12. **Arquivo corrompido** — downloads com menos de 8 KB são tratados como truncados e descartados na hora. Se o script insistir em rebaixar o mesmo arquivo várias vezes numa conexão ruim, é isso acontecendo.
13. **Uma instância por pasta** — `satelite_temp_downloads/` (cache de `.nc` e do fundo GLM) não é isolado por assinatura de render como `satelite_temp_images/<assinatura>/` é, e é apagado por inteiro ao final de cada execução. Rodar duas instâncias do script ao mesmo tempo na mesma pasta faz uma apagar o cache compartilhado da outra em pleno voo. O script trava isso sozinho: a segunda instância recebe um erro fatal imediato ao tentar iniciar, antes de baixar qualquer coisa. Para rodar duas gerações em paralelo, use pastas (cópias do repositório) separadas.
14. **Retomada de vídeo valida o PNG, não só a existência dele** — se uma execução anterior for interrompida no meio da gravação de um frame (Ctrl+C, falta de energia, OOM), o cache de retomada do modo vídeo detecta o PNG truncado e re-renderiza o frame em vez de aceitá-lo cego; o mesmo vale na hora de montar o `.mp4` final, onde frames corrompidos são descartados com aviso em vez de derrubar a montagem inteira.

---

## Pré-requisitos

- Python 3.10+
- FFmpeg instalado e no `PATH` do sistema (obrigatório se `generation_type = V`)

## Instalação

```bash
git clone https://github.com/ArthurPH25/goes-image-generator
cd goes-image-generator
```

### Dependências de sistema (Cartopy)

`cartopy` depende de **GEOS** e **PROJ**, que não vêm pelo pip. Sem elas instaladas antes, `pip install cartopy` falha ao compilar.

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install libgeos-dev libproj-dev proj-bin proj-data
```

**Fedora:**
```bash
sudo dnf install geos-devel proj-devel proj-data
```

**macOS (Homebrew):**
```bash
brew install geos proj
```

**Windows:** use [Anaconda/Miniconda](https://docs.conda.io/en/latest/miniconda.html) e instale o cartopy via conda-forge — ele resolve os binários sozinho:
```bash
conda install -c conda-forge cartopy
```
Isso não substitui o FFmpeg — ele continua precisando ser instalado à parte e adicionado ao `PATH` (veja abaixo).

### Dependências Python

```bash
pip install -r requirements.txt
```

### FFmpeg (obrigatório para `generation_type = V`)

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows: baixe em https://ffmpeg.org/download.html e adicione a pasta bin/ ao PATH
```

Confirme com `ffmpeg -version`.

### Execução headless (servidor, container, SSH sem X11)

Roda sem problemas em servidor sem interface gráfica — `matplotlib` já usa o backend `Agg` em todos os módulos. A única etapa que depende de ambiente gráfico é a abertura automática do vídeo ao final do modo `V` (`xdg-open`/`open`/`os.startfile`); em ambiente headless isso só falha silenciosamente com um aviso no console — o `.mp4` já está salvo em `satelite_videos/`.

---

## Uso

1. Edite `config.ini` e `colors.ini` na raiz do projeto (canal, data/hora, recorte, cores — ver [Configuração](#configuração)).
2. Rode:
```bash
python index.py
```
3. Acompanhe pelo console: busca no S3, download, processamento dos frames e (se `V`) montagem do vídeo.

### Saída

| Pasta | Conteúdo |
|---|---|
| `satelite_images/` | PNGs do modo `generation_type = I` |
| `satelite_videos/` | Vídeos `.mp4` do modo `generation_type = V` |
| `satelite_temp_images/<assinatura>/` | Frames PNG intermediários do modo vídeo, isolados por combinação de parâmetros visuais; mantido se `delete_temp_images = False` |
| `satelite_temp_downloads/` | Cache temporário de `.nc` baixados (NOAA + fundo ABI/GLM), limpo ao final da execução |

Ao final, o console mostra o tempo de execução por etapa e um resumo de todos os avisos/erros não fatais da execução, incluindo os que rodaram nos workers.

---

## Licença

[MIT License](LICENSE) — use, copie, modifique, distribua ou venda à vontade, com ou sem crédito, desde que o aviso de copyright original seja mantido. Fornecido "como está", sem garantias.