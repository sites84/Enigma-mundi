# Enigma Mundi

Portal editorial sobre mistérios, fenômenos inexplicados, ufologia, lendas urbanas, folclore e teorias da conspiração.

## Estado do projeto

Primeira etapa: estrutura inicial do site e inventário de fontes. O repositório estava vazio no início da implementação.

## Princípios editoriais

- A página inicial lista primeiro as publicações mais recentes.
- Alegações, relatos, hipóteses e fatos documentados devem ser distinguidos.
- Fontes e links de origem devem ser preservados nos artigos.
- Conteúdo RSS serve como sinal de descoberta; não autoriza republicação integral.
- Imagens de terceiros só devem ser republicadas quando os direitos/licença permitirem. Caso contrário, usar imagem licenciada ou autorizada.
- A ingestão precisa ser idempotente: uma mesma matéria não deve ser publicada duas vezes.
- Publicação automatizada depende de configuração explícita de provedor de IA e de credenciais via secrets; nunca salvar chaves no repositório.

## Fontes

O inventário em `config/sources.json` registra os domínios fornecidos. Nem todo domínio informado é um endpoint RSS válido. As fontes devem ser verificadas individualmente antes de ativar a coleta automática.

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

Esta etapa é **somente descoberta**. Não reescreve nem publica artigos. Isso é intencional: a geração editorial precisa de um provedor de IA configurado via GitHub Secrets, verificação de fontes, deduplicação editorial e validação do conteúdo antes de permitir publicação. Erros de acesso são registrados para diagnóstico; sites com JavaScript, bloqueios ou estruturas especiais podem exigir adaptadores específicos.

## Estrutura editorial dos artigos

Cada artigo produzido deverá conter:
- Reportagem original em português, sem copiar extensamente a matéria de origem.
- Atribuição e links para a fonte original e para as referências consultadas.
- Separação explícita entre fatos documentados, relatos, hipóteses e especulação.
- Uma seção **“O que pode ser?”**, específica para o caso, com explicações científicas plausíveis, evidências que apoiam ou contradizem cada hipótese e limites do que é possível concluir.
- Referências verificáveis para alegações científicas. Se não houver evidência suficiente, o texto deve declarar a incerteza em vez de inventar uma explicação.
- Imagens e vídeos apenas quando houver URL, licença e condições de uso verificáveis; não copiar mídia de terceiros automaticamente.

A publicação automática só deve ser ativada depois de validar a fila de descoberta, a extração de texto, a checagem de fatos e a renderização dos artigos. Nenhuma fonte precisa ter `enabled: true` para entrar na fila de descoberta; esse campo deve controlar a publicação, não impedir a investigação técnica da fonte.
