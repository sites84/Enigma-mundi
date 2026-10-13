# Enigma Mundi

Portal editorial sobre mistérios, fenômenos inexplicados, ufologia, lendas urbanas, folclore e teorias da conspiração.

## Estado do projeto

Primeira etapa: estrutura inicial do site e inventário de fontes. O repositório estava vazio no início da implementação.

## Princípios editoriais

- A página inicial lista primeiro as publicações mais recentes.
- Alegações, relatos, hipóteses e fatos documentados devem ser distinguidos.
- Fontes e links de origem devem ser preservados nos artigos.
- Conteúdo RSS serve como sinal de descoberta; não autoriza republicação integral.
- As imagens podem ser selecionadas pela relevância para a matéria; guardar URL da imagem, URL da página de origem e texto alternativo. A ausência de metadados de licença não deve bloquear a descoberta ou a preparação do artigo.
- A ingestão precisa ser idempotente: uma mesma matéria não deve ser publicada duas vezes.
- Publicação automatizada depende de configuração explícita de provedor de IA e de credenciais via secrets; nunca salvar chaves no repositório.

## Fontes

O inventário em `config/sources.json` reúne 41 fontes: 21 fontes preexistentes e 20 fontes internacionais adicionadas, com foco em mistérios, ufologia, ciência, folclore, arqueologia e investigação cética. O workflow valida feeds RSS/Atom e registra o estado de cada fonte em `data/source-status.json`; fontes sem feed podem continuar elegíveis para descoberta por sitemap e HTML.

## Próximas etapas

1. Validar o site estático e a estrutura visual.
2. Identificar RSS/Atom reais e status de cada fonte.
3. Implementar ingestão, deduplicação, extração de metadados e registro de erros.
4. Implementar geração editorial com checagens e revisão configurável.
5. Implementar armazenamento e publicação automática com proteção contra duplicatas.
6. Adicionar sitemap, metadados SEO, monitoramento e testes.

## Desenvolvimento local

A primeira versão da interface é estática e não exige dependências. Abra `index.html` no navegador ou publique o repositório no GitHub Pages.

## Coleta diária sem dependência de RSS

O workflow `.github/workflows/crawl-sources.yml` roda diariamente e também pode ser iniciado manualmente em GitHub Actions. O script `scripts/crawl_sources.py` procura artigos em sitemaps XML e links HTML da página inicial, respeitando as regras de `robots.txt` quando consegue lê-las. Ele registra URLs e metadados em `data/discovered-articles.json`, com deduplicação por URL normalizada.

Esta etapa é **somente descoberta**. Não reescreve nem publica artigos. As 20 novas fontes internacionais passam pelo mesmo validador e pela mesma fila de descoberta; a validação diária identifica feeds indisponíveis ou que precisem de adaptadores. Isso é intencional: a geração editorial precisa de um provedor de IA configurado via GitHub Secrets, verificação de fontes, deduplicação editorial e validação do conteúdo antes de permitir publicação. Erros de acesso são registrados para diagnóstico; sites com JavaScript, bloqueios ou estruturas especiais podem exigir adaptadores específicos.

## Estrutura editorial dos artigos

Cada artigo produzido deverá conter:
- Reportagem original em português, sem copiar extensamente a matéria de origem.
- Atribuição e links para a fonte original e para as referências consultadas.
- Separação explícita entre fatos documentados, relatos, hipóteses e especulação.
- Uma seção **“O que pode ser?”**, específica para o caso, com explicações científicas plausíveis, evidências que apoiam ou contradizem cada hipótese e limites do que é possível concluir.
- Referências verificáveis para alegações científicas. Se não houver evidência suficiente, o texto deve declarar a incerteza em vez de inventar uma explicação.
- Imagens e vídeos devem manter URL da mídia, página de origem e texto alternativo quando disponíveis. A falta de metadados de licença não bloqueia a preparação de uma matéria.

A publicação automática só deve ser ativada depois de validar a fila de descoberta, a extração de texto, a checagem de fatos e a renderização dos artigos. Nenhuma fonte precisa ter `enabled: true` para entrar na fila de descoberta; esse campo deve controlar a publicação, não impedir a investigação técnica da fonte.


## Geração automática de reportagens originais

O workflow `.github/workflows/editorial-pipeline.yml` processa até três candidatas por execução, extrai o texto disponível da página original e usa um provedor compatível com a API Chat Completions para criar uma reportagem nova em português. Os artigos são gravados em `artigos/` e catalogados em `data/editorial-posts.json`; a página inicial prioriza essas reportagens originais, mantendo também a lista de fontes para apuração.

Para ativar a geração, adicione o segredo `GEMINI_API_KEY` em **Settings → Secrets and variables → Actions → New repository secret**. É a opção recomendada para começar com a API do Google AI Studio; configure opcionalmente a variável `GEMINI_MODEL` (padrão `gemini-3.8-flash`). O script também aceita `OPENAI_API_KEY` como alternativa, com `OPENAI_MODEL` opcional (padrão `gpt-4o-mini`). A chave nunca deve ser inserida nos arquivos do repositório. Sem nenhuma chave, o workflow não gera conteúdo e grava o estado `waiting_for_api_key` em `data/editorial-pipeline-status.json`.

O pipeline rejeita páginas com pouco texto extraído e valida a estrutura mínima da reportagem. Cada página informa a fonte consultada e deixa claro que uma única fonte não representa confirmação independente. A publicação automatizada não inventa fontes adicionais nem afirma que houve verificação independente. O limite padrão é de três artigos por execução, ajustável pela variável de ambiente `MAX_ARTICLES_PER_RUN`.
