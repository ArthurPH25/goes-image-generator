# GOES Satellite Image Generator

Gerador de imagens e vídeos de satélite a partir de dados brutos GOES-16/GOES-19 (NOAA), com foco em cobertura da **América do Sul**. Renderiza qualquer uma das 16 bandas espectrais do instrumento ABI e o mapeador de raios GLM, com paletas de cores, projeção cartográfica e estilo de saída totalmente configuráveis.

## Visão Geral

O projeto baixa arquivos NetCDF diretamente do bucket público S3 da NOAA (`noaa-goes16` / `noaa-goes19`), recorta espacialmente os dados para a região de interesse, aplica a paleta de cores correspondente ao canal e renderiza o resultado com `matplotlib` + `cartopy`. A saída pode ser uma imagem única (`.png`) ou um vídeo (`.mp4`, montado via `ffmpeg` a partir de uma sequência de frames).

### Arquitetura

O fluxo de execução é orquestrado por `index.py`:

1. **Validação de configuração:** `config.ini` e `colors.ini` são lidos e validados de forma estrita antes de qualquer processamento (sem valores padrão implícitos; chave ausente ou malformada interrompe a execução).
2. **Descoberta de arquivos:** para cada timestamp alvo, o bucket S3 correspondente ao satélite ativo naquele período (GOES-16 até 07/04/2025, GOES-19 depois) é listado via `s3fs` para localizar o arquivo NetCDF do canal solicitado.
3. **Download concorrente:** `S3_downloader.py` baixa os arquivos de forma assíncrona (`asyncio` + `s3fs`), com retry exponencial, validação de integridade do NetCDF e tratamento diferenciado de erros transitórios vs. definitivos (404).
4. **Processamento e renderização:** dividido em dois módulos especializados:
   - `goes_bands.py`: extrai e renderiza qualquer uma das bandas ABI (canais 1–16), aplicando recorte geográfico, colormap (refletância em escala de cinza ou paleta térmica customizada) e overlay de mapa.
   - `GLM.py`: processa dados de densidade de raios do GLM, com suporte a rastro histórico configurável (idade do raio codificada em cor e tamanho do marcador) e composição opcional sobre uma banda ABI de fundo (com cache compartilhado entre frames de um mesmo vídeo).
5. **Paralelização:** múltiplos frames são processados simultaneamente via `ProcessPoolExecutor` (paralelismo de CPU), enquanto os downloads de cada frame usam concorrência assíncrona internamente (paralelismo de I/O).
6. **Montagem de vídeo:** quando `generation_type = V`, os frames gerados são costurados em `.mp4` via `imageio`/`ffmpeg`, com controle de bitrate (CRF), preset de compressão e escala de saída.

`utils.py` concentra as funções compartilhadas entre os módulos: parsing e validação de configuração, geometria de projeção geoestacionária, recorte de área, composição dos eixos do mapa, título, marca d'água e persistência do cache de fundo do GLM (com lock de arquivo para evitar reprocessamento concorrente do mesmo frame por workers diferentes).

## Foco Regional: América do Sul

Este software não é um gerador genérico de imagens GOES, as decisões de projeto são voltadas especificamente à operação sobre a América do Sul:

- **Satélite de referência**: GOES-19 (posição orbital em -75°) é o satélite operacional padrão para cobertura do continente sul-americano desde abril de 2025; o script alterna automaticamente para GOES-16 em datas anteriores a essa transição.
- **Cobertura geográfica**: a região-alvo (`target_coordinates` em `config.ini`) é definida em coordenadas geográficas (oeste, leste, sul, norte), permitindo recortar com precisão qualquer sub-região do continente, reduzindo drasticamente o volume de dados processado em comparação ao disco completo do satélite.
- **Fuso horário**: todos os horários de entrada e saída (busca no bucket, timestamps de imagem/vídeo, título das figuras) operam em **UTC**, evitando ambiguidade ao correlacionar com horários locais da América do Sul (UTC-3, UTC-4, UTC-5, conforme o país/estação).
- **Cadência de dados**: os intervalos de captura (10 minutos para ABI, 20 segundos para GLM) refletem a cadência do modo Full Disk do satélite, que é o que garante cobertura contínua de todo o continente.

## Configuração

Toda a parametrização do sistema: canal, tipo de geração, período, workers, projeção, coordenadas, paletas de cor, estilo visual e parâmetros de codificação do vídeo, é feita através de dois arquivos:

- **`config.ini`:** parâmetros gerais de execução, mapa e renderização.
- **`colors.ini`:** paletas de cores por banda espectral e pelo GLM.

Cada chave está **documentada diretamente no próprio arquivo `.ini`**, incluindo formato esperado, valores válidos e regras de negócio. Consulte os comentários desses arquivos antes de alterar qualquer parâmetro, não há valores padrão: uma chave ausente ou fora do formato esperado interrompe a execução imediatamente.

### Pontos de atenção ao personalizar

- **Trocar `channel` (ABI ↔ GLM) exige revisar o formato de horário junto.** ABI usa `HH:MM` (sem segundos); GLM usa `HH:MM:SS` (com segundos). `image_time` (ou `start_time`/`end_time` em modo vídeo) precisa estar no formato do canal **novo**, não do anterior — o script não converte automaticamente e para com erro de formato se você esquecer.
- **`target_coordinates` não é validado quanto à ordem dos valores.** O script confere que são 4 números, mas não confere se Oeste < Leste e Sul < Norte. Valores fora de ordem não geram erro na validação inicial — o mapa sai incorreto lá na frente.
- **`glm_flash_age = False` remove o rastro de raios, não só a cor.** Com `False`, cada frame mostra apenas os raios daquele instante exato (sem histórico, sem legenda); as chaves `glm_history_lookback_steps`/`glm_history_max_concurrent_downloads` ficam sem efeito nesse modo.
- **`glm_background_band` preenchido muda o nome do arquivo de saída** (sufixo `_CXX.png`). Alternar essa chave entre execuções pode deixar arquivos de nomes diferentes para o mesmo horário nas pastas de cache/saída. Além disso, um valor inválido nessa chave (fora de 1–16, ou não numérico) só é detectado **depois** de baixar o arquivo GLM principal daquele frame — não na validação inicial.
- **O fundo ABI usado com o GLM pode estar até ~10 minutos atrasado em relação ao raio mostrado, sempre.** O ABI só captura a cada 10 minutos, mas o GLM a cada 20 segundos; o script sempre arredonda o horário do GLM **para trás** até o múltiplo de 10 anterior para escolher o fundo (ex.: um raio às 19:19:40 é sobreposto ao fundo ABI das 19:10, nunca das 19:20). Isso é o comportamento normal e sempre acontece, não é uma falha eventual — o fundo nunca representa exatamente o mesmo instante do raio, e as nuvens no fundo podem parecer "atrasadas" em relação à posição real dos raios.
- **Vídeo de GLM com fundo ABI + `num_workers` alto:** o fundo de cada bloco de 10 minutos é gerado uma vez e compartilhado entre os frames GLM daquele bloco via um cache com trava entre processos. Em concorrência muito alta, um worker pode esperar até 5 minutos pelo fundo e, se estourar esse tempo, gerar aquele frame silenciosamente **sem** fundo (aviso no console, sem interromper a execução). Se notar frames sem fundo intercalados no vídeo, reduza `num_workers`.
- **Trocar `figure_width`/`figure_height` no meio de um cache de frames existente (`satelite_temp_images` com `delete_temp_images = False`) pode gerar um vídeo com frames de proporções diferentes.** O vídeo usa as dimensões do *primeiro* frame como referência e redimensiona (esticando/comprimindo) qualquer frame de tamanho diferente para bater — isso inclui frames antigos do cache renderizados com outra `figure_width`/`figure_height`. Se for testar tamanhos de figura diferentes, limpe `satelite_temp_images` antes de gerar o vídeo final.
- **A faixa do título (quando `clean_mode = False`) tem altura fixa em polegadas**, então ocupa uma fração maior ou menor da imagem dependendo de `figure_height`. Isso é esperado, não um bug: ajuste `figure_height` para reequilibrar visualmente se o título parecer grande/pequeno demais.
- **Um timestamp "múltiplo de 10/20" válido no formato pode ainda não existir no bucket da NOAA** (falha de scan do satélite). Isso aparece como "arquivo não encontrado", não como erro de configuração — no modo vídeo, o frame é apenas pulado.
- **Arquivos baixados menores que 8 KB são tratados como corrompidos/truncados** e descartados automaticamente (com nova tentativa, até o limite de retries). Isso é o motivo mais provável de um download ser re-tentado "sem razão aparente" em conexões instáveis — não é erro de configuração.

## Pré-requisitos

- Python 3.10+
- FFmpeg instalado e acessível no `PATH` do sistema (necessário apenas para `generation_type = V`)

## Instalação


```

bash
git clone 
cd 

```

Recomenda-se o uso de um ambiente virtual:


```

bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

pip install -r requirements.txt

```

> `cartopy` depende de bibliotecas geoespaciais nativas (GEOS, PROJ). Caso a instalação via `pip` falhe, utilize `conda`/`mamba` (`conda install -c conda-forge cartopy`) ou instale as dependências de sistema correspondentes à sua distribuição.

## Uso

> **Nota:** `config.ini` e `colors.ini` são sempre lidos a partir da pasta onde `index.py` está salvo, não da pasta de onde você executa o comando. Ou seja, se você rodar `python /algum/caminho/index.py` estando em outro diretório, o script ainda vai procurar os `.ini` ao lado do próprio `index.py`, e não no seu diretório atual.

1. Edite `config.ini` para definir o canal (`GENERAL.channel`), o tipo de geração (`GENERAL.generation_type`: `I` para imagem única ou `V` para vídeo), o período desejado e a região de interesse (`MAP.target_coordinates`). **Se for trocar o canal entre ABI e GLM, lembre-se de ajustar também o formato de `image_time` (ou `start_time`/`end_time`) — veja "Pontos de atenção" abaixo.**
2. Se necessário, ajuste as paletas de cor em `colors.ini` para o canal escolhido.
3. Execute:


```

bash
python index.py

```

A saída é gerada automaticamente em:

- `satelite_images/`: imagens únicas (`generation_type = I`)
- `satelite_videos/`: vídeos finalizados (`generation_type = V`)
- `satelite_temp_images/`: frames intermediários do vídeo (removidos ao final se `delete_temp_images = True`)
- `satelite_temp_downloads/`: cache temporário de arquivos NetCDF baixados

O progresso, incluindo busca de arquivos no bucket, download, renderização e (quando aplicável) codificação do vídeo, é reportado no console, com um relatório final de tempo de execução por etapa.