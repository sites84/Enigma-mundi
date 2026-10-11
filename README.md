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