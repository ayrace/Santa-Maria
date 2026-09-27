# Painel Geográfico de Nodes — Santa Maria

Primeira versão baseada no padrão operacional já consolidado em Pelotas e Rio Grande.

## Fonte operacional

A base geográfica vem do arquivo `SANTA MARIA COMPLETO.kml` e a coleta do XPERTrack usa o formato:

`Node | Pontuação | Impactado | Estressado | Total`

Regras:

- Pontuação `0` = porta OFF.
- Pontuação `1–20` = porta crítica; permanece nos detalhes e não cria uma quarta cor principal.
- Pontuação `>20` = porta online.
- Verde = node Online, sem porta zerada.
- Amarelo = SS Parcial, pelo menos uma porta zerada e pelo menos uma ainda ativa.
- Vermelho = SS Total, todas as portas existentes zeradas.

## Google Drive

A pasta de Santa Maria foi criada em:

`https://drive.google.com/drive/folders/1TkKRryLA1Tk5XZ2OTFUPPBtjdBMHXpss`

Coloque nela a coleta com o nome exato:

`SANTA MARIA.csv`

O painel verifica a pasta pública, encontra o arquivo pelo nome e força nova leitura a cada minuto com `no-cache`. A melhor rotina é manter o mesmo arquivo no Drive e atualizar sua versão/conteúdo, sem trocar o nome.

Enquanto o CSV ainda não estiver no Drive, esta versão usa `data/SANTA MARIA.csv` como fallback para o primeiro teste.

## Base geográfica validada automaticamente

- 87 nodes lógicos encontrados na coleta atual.
- 84 com localização resolvida pelo KML/âncoras seguras.
- 3 pendentes de confirmação: `CMBAO-CNTAV`, `CNTBN`, `JOBIM`.
- Cadastros A/B que compartilham um ponto antigo do KML permanecem identidades separadas. O marcador é deslocado apenas visualmente alguns metros para permitir clique individual; a rota no Google Maps usa a coordenada original do KML.

## Publicar

1. Crie um repositório GitHub para Santa Maria.
2. Envie todo o conteúdo desta pasta, preservando `data/` e `.streamlit/`.
3. No Streamlit Community Cloud, crie o app apontando para `streamlit_app.py`.
4. Depois do primeiro deploy, valide os nodes pendentes e eventuais correções de identidade/localização.

## Outages

O arquivo `data/outages_atual.csv` começa com zero. O bloco de outages fica oculto quando o valor é zero, seguindo o padrão final adotado em Rio Grande.
