# GOES Image Generator

Gerador de imagens e vídeos de alta precisão a partir de dados brutos dos satélites GOES-16 e GOES-19 (NOAA), totalmente otimizado para a **América do Sul**. Renderiza todas as 16 bandas espectrais do sensor ABI e o mapeador de raios GLM com suporte a colormaps, projeção cartográfica e estilização 100% customizáveis.

---

## Visão Geral

O script pega os arquivos NetCDF direto do bucket público S3 da NOAA (`noaa-goes16` / `noaa-goes19`), faz o recorte geográfico da região escolhida, aplica a paleta de cores correspondente e renderiza tudo usando `matplotlib` + `cartopy`.

A saída pode ser uma imagem avulsa em PNG ou um vídeo em MP4 (costurado via `ffmpeg` a partir da sequência de frames).

### Arquitetura do Sistema

A orquestração principal roda em `index.py`:

1. **Validação:** `config.ini` (seções `[GENERAL]`, `[IMAGE_TIME]` / `[VIDEO_TIME]`) e o canal são parseados e validados logo na arrancada. Esqueceu uma chave básica ou digitou horário errado? O script capota imediatamente antes de baixar qualquer coisa. *(Nota: validações de mapa, estilo e cores rodam no worker no início da renderização).*
2. **Pegando os Arquivos no S3:** Localização dos arquivos NetCDF no bucket correspondente. O script alterna automaticamente entre satélites: **GOES-16** até 07/04/2025 e **GOES-19** para datas posteriores.
3. **Download Assíncrono Concorrente:** `S3_downloader.py` baixa a parada usando `asyncio` + `s3fs`, com retry exponencial, checagem de integridade e tratamento pra não travar em erro 404 de imagem que não existe no bucket.
4. **Processamento e Renderização:**
   - `goes_bands.py`: cuida das 16 bandas ABI (refletância em cinza ou paleta térmica customizada) com recorte de mapa e overlays.
   - `GLM.py`: processa a densidade de raios do GLM, com rastro histórico (idade codificada por cor e tamanho de marcador) e sobreposição opcional em fundo ABI (com cache compartilhado entre frames).
5. **Paralelização:** processamento de frames via `ProcessPoolExecutor` (paralelismo de CPU), enquanto os downloads de cada frame rodam concorrentemente via `asyncio` (paralelismo de I/O).
6. **Montagem do Vídeo:** se `generation_type = V`, o `imageio`/`ffmpeg` entra em ação costurando os frames em `.mp4` com controle de bitrate (CRF), preset e escala.
7. **Encerramento:** ao final (mesmo em caso de erro não fatal), o script imprime dois relatórios: o de tempo de execução por etapa, e um relatório consolidado de todos os avisos (⚠️) e erros (❌) ocorridos durante a busca de arquivos e o processamento dos frames, inclusive os que rodaram nos workers paralelos. Erros fatais (☠️) não entram nesse relatório porque eles já interrompem o script na hora em que acontecem.

O arquivo `utils.py` segura a bronca das funções compartilhadas: validações, projeções geoestacionárias, recortes de área, marca d'água, títulos e a trava de arquivo (file lock) pra evitar que dois workers engalfinhem tentando processar o mesmo fundo de GLM ao mesmo tempo.

---

## Foco Regional: América do Sul

Isso aqui não foi feito pra ser mais um gerador genérico. Cada linha de código foi pensada pra operar na América do Sul sem passar raiva:

- **Satélite de Referência:** O GOES-19 (na posição orbital 75°W) é o satélite oficial cobrindo o continente desde abril de 2025. Datas anteriores usam automaticamente o GOES-16 sem você precisar mexer em nada.
- **Recorte Geográfico:** A chave `target_coordinates` usa coordenadas geográficas (Oeste, Leste, Sul, Norte). Você recorta exatamente a tempestade ou o estado que quer analisar, economizando processamento de CPU e memória RAM ao renderizar apenas a área de interesse, além de gerar arquivos de saída bem mais leves.
- **Fuso Horário:** **Tudo opera em UTC.** Busca no bucket, timestamps das imagens, títulos e nomes de arquivos. Zero dor de cabeça com fusos do Brasil (UTC-3, UTC-4, UTC-5).
- **Cadência do Satélite:** Os intervalos do script seguem o modo Full Disk real: 10 minutos para bandas ABI e 20 segundos para dados de raios GLM.

---

## Configuração

O comportamento do script é controlado por dois arquivos `.ini` principais:

- **`config.ini`:** regras gerais, período, workers, projeções, recortes, GLM e renderização do vídeo.
- **`colors.ini`:** tabelas de cores hexadecimais por banda térmica e idade de raio do GLM.

> **Regra Importante:** Não existem valores padrão implícitos no código. Se você apagar uma chave ou mandar um valor bizarro nas seções de tempo/canal, a validação inicial interrompe o script na hora. Se a cagada for em `[MAP]`, `[STYLE]` ou `colors.ini`, o erro estoura no primeiro worker que tentar renderizar o frame.

### Pontos de Atenção e Gambiarras

1. **Formato de Horário ABI vs. GLM:**
   - ABI usa `HH:MM` (ex: `22:30`).
   - GLM usa `HH:MM:SS` (ex: `22:30:20`).
   Se trocar a chave `channel` em `config.ini`, **você é obrigado a ajustar a chave de horário**, senão a validação quebra no seu colo.
2. **Ordem das Coordenadas:** O script confere se você passou 4 números em `target_coordinates`, mas **não** checa se você colocou Oeste < Leste e Sul < Norte. Se invertê-los, a validação passa e o mapa sai cagado na renderização.
3. **`glm_flash_age = False` desliga tudo:** Se colocar `False`, o script mostra apenas os raios do segundo exato do frame. O rastro histórico morre, a legenda desparece e as chaves de histórico (`glm_history_lookback_steps`) ficam sem efeito.
4. **Sufixo de Cache `_CXX` no GLM:** Preencher `glm_background_band` adiciona o sufixo da banda no nome do frame (ex: `_C13.png`). Alternar essa chave no meio de testes vai duplicar arquivos na sua pasta temporária. Além disso, se você colocar uma banda inválida (fora de 1 a 16), o erro só vai estourar **depois** que o script já tiver baixado o arquivo GLM principal.
5. **Lag no Fundo do GLM (Isso é normal!):** O ABI só atualiza a cada 10 min, mas o GLM anda de 20 em 20 segundos. O script sempre arredonda o horário do GLM **para trás** pro múltiplo de 10 anterior. Um raio das 19:19:40 vai rodar em cima do fundo ABI das 19:10 (lag de até 9 min e 40s). Não é bug, é limitação da física do satélite.
6. **Estouro de Lock em Vídeos GLM:** Em vídeos GLM com fundo ABI e `num_workers` muito alto (tipo 12+), um worker pode ficar esperando a renderização do fundo por mais de 5 minutos. Se estourar o limite, o frame sai sem fundo e manda um aviso amarelo (⚠️) no console. Deu esse problema? Baixe o `num_workers`.
7. **Cache de Frames com Resoluções Diferentes:** Se você mudar `figure_width` ou `figure_height` mantendo `delete_temp_images = False`, os frames antigos do cache vão ter tamanhos diferentes dos novos. O script (via Pillow) vai redimensionar os frames pra baterem com a dimensão do primeiro frame gerado, esticando ou deformando a imagem final no vídeo. Mudou a resolução? Limpe a pasta `satelite_temp_images`.
8. **Proporção do Título:** A barra superior do título tem altura fixa em polegadas (quando `clean_mode = False`). Se a imagem parecer "engolida" pelo título, aumente o `figure_height`.
9. **Imagens Faltantes na NOAA:** Nem todo timestamp existe no bucket da NOAA (falha de varredura). No modo vídeo (`generation_type = V`), o frame com falha é simplesmente pulado e o script avisa no console; no modo imagem única (`generation_type = I`), se o horário pedido não for encontrado, o script encerra com erro fatal.
10. **Filtro de Arquivo Corrompido:** Downloads com menos de 8 KB são tratados como arquivo truncado/corrompido e descartados na hora. Se o script ficar tentando baixar o mesmo arquivo várias vezes em conexões ruins, é isso acontecendo.

---

## Pré-requisitos

- **Python 3.10+**
- **FFmpeg** instalado e adicionado ao `PATH` do sistema (obrigatório se `generation_type = V`).

---

## Instalação

Clone o repositório e acesse a pasta:

```bash
git clone https://github.com/ArthurPH25/goes-image-generator
cd goes-image-generator
```

### Dependências de sistema (Cartopy)

O `requirements.txt` cobre as bibliotecas Python, mas o `cartopy` depende de duas bibliotecas nativas — **GEOS** e **PROJ** — que não vêm pelo pip. Sem elas instaladas no sistema *antes*, o `pip install cartopy` falha na hora de compilar.

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

**Windows:** o jeito mais tranquilo é usar o [Anaconda/Miniconda](https://docs.conda.io/en/latest/miniconda.html) e instalar o cartopy via conda-forge (ele já resolve os binários):
```bash
conda install -c conda-forge cartopy
```

Se você já usa conda em qualquer sistema, instalar o cartopy por ele evita esse tipo de dor de cabeça.

### Ambientes headless / Docker

O script roda sem problemas em servidor ou container sem interface gráfica — `matplotlib` já está configurado com o backend `Agg` (renderização sem tela) em todos os módulos. A única etapa que depende de ambiente gráfico é a abertura automática do vídeo ao final do modo `V` (`xdg-open`/`open`/`os.startfile`); num ambiente headless isso simplesmente não tem o que abrir, o script avisa no console e segue normal — o `.mp4` já está salvo em `satelite_videos/` de qualquer forma.

### Instalando as dependências Python

Com as bibliotecas de sistema prontas:

```bash
pip install -r requirements.txt
```

Garanta também que o **FFmpeg** esteja instalado e no `PATH` (obrigatório para `generation_type = V`):

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows: baixe em https://ffmpeg.org/download.html e adicione a pasta bin/ ao PATH
```

Verifique com `ffmpeg -version`.

---

## Uso

1. Edite `config.ini` e `colors.ini` na raiz do projeto com os parâmetros desejados (canal, data/hora, recorte geográfico, cores, etc — veja a seção [Configuração](#configuração) acima).
2. Rode o script:

```bash
python index.py
```

3. Acompanhe o progresso pelo console: busca dos arquivos no S3, download, processamento dos frames e (se `generation_type = V`) montagem do vídeo.

### Saída gerada

O script cria as seguintes pastas na raiz do projeto conforme a necessidade:

| Pasta | Conteúdo |
|---|---|
| `satelite_images/` | PNGs gerados no modo `generation_type = I` (imagem única) |
| `satelite_videos/` | Vídeos `.mp4` gerados no modo `generation_type = V` |
| `satelite_temp_images/` | Frames PNG intermediários usados para montar o vídeo (mantidos se `delete_temp_images = False`) |
| `satelite_temp_downloads/` | Cache temporário de arquivos `.nc` baixados da NOAA e do fundo ABI/GLM (limpo automaticamente ao final da execução) |

Ao final, o script imprime dois relatórios no console: tempo de execução por etapa (busca, download/processamento, montagem do vídeo) e um resumo consolidado de todos os avisos (⚠️) e erros (❌) não fatais ocorridos durante a execução, incluindo os que rodaram nos workers paralelos.

---

## Licença

Este projeto está licenciado sob a [MIT License](LICENSE) — use, copie, modifique, distribua ou venda à vontade, com ou sem crédito obrigatório em qualquer parte do software, desde que o aviso de copyright original seja mantido. O software é fornecido "como está", sem garantias de qualquer tipo.