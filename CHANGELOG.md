# Changelog

## [0.3.0] - 2026-11-27

### Added

- Contrato JSON de review v1 estrito via `--from-json PATH`: `schema`,
  `source` (`kind`, `ref`), `head`, `verdict`, `reviewers` e `intent`. Chaves
  desconhecidas, duplicadas e JSON malformado reprovam; o `head` do contrato é
  um guard e precisa ser HEAD ou um ancestral com a mesma árvore, e o tree é
  sempre calculado pelo Atesto, nunca lido do JSON.
- Fonte GitHub PR (`--from-github-pr N`): lê o head e as reviews pelo `gh`,
  conta só `APPROVED` cujo commit é o head do PR, mantém a última review por
  login e reprova em `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, head divergente,
  CLI ausente/sem autenticação, saída malformada e erro HTTP.
- Fonte GitLab MR (`--from-gitlab-mr IID`): exige o SHA do MR igual ao HEAD,
  usa `approved_by`, reprova em `requested_changes`, `not_approved`,
  discussões bloqueantes, 401/403 e CLI ausente/sem autenticação.
- `source` opcional em `review.json` (`github`, `gitlab` ou `json`), validado
  pelo `check` e exposto em `check --json`; artefatos antigos sem `source`
  continuam válidos.
- Imagem de container `ghcr.io/guihirsch/atesto`: Python 3.12 slim pinado por
  digest, `git`, wheel pinado por sha256, usuário não-root, `WORKDIR /repo` e
  `ENTRYPOINT ["atesto"]`. Labels gravam a versão e o sha256 do wheel.
  Workflow de smoke em mudanças de container e publicação da imagem no
  release (tags de versão e `latest`).
- Snippets aditivos para GitLab CI, GitHub Actions, Jenkins, CircleCI e
  Buildkite em `integrations/ci/`: histórico completo, base ref e
  `check --ci` na imagem pinada, sem gerar ou commitar evidência. O exemplo
  Jenkins ganhou `jenkins.stage.groovy`, um fragmento de stage para inserir em
  um pipeline existente.
- README: quickstart até `atesto: ok`, threat model, matriz host/CI,
  contrato JSON v1 completo (campos, exemplo, head guard e modos de falha),
  limites de aprovação stale do GitLab, política de bots/`reviewDecision` do
  GitHub, pin por digest e verificação de release (wheel e imagem publicados,
  hashes e rebuild com `umask 022`).
- `check --json` agora expõe a `source` da review; `doctor` continua
  reportando a identidade da ferramenta e os problemas dos artefatos.

### Changed

- Versão 0.3.0. O schema de config não muda: v1 e v2 continuam iguais e
  `atesto init` continua escrevendo v2.
- Adaptadores de review de provedor saíram do core para o pacote opcional
  `atesto.review_sources` (`base`, `github`, `gitlab`), importado pelo CLI só
  quando o flag correspondente é usado. O core mantém o writer de review,
  `check` e `--from-json`, e não codifica política de merge de provedor.
  Aprovações de bot no GitHub não contam para `min_reviewers`.
- Template de CI pinado em `atesto==0.3.0` com sha256; o pin do template é a
  única fonte do hash. `Dockerfile` e `container-publish.yml` exigem
  versão/hash como input e não têm default.
- `release.yml` confere o wheel construído contra o pin do template **antes**
  do PyPI, usa `umask 022` e escopa `id-token: write` ao job de publish. O job
  de container baixa o wheel publicado, revalida o hash, builda/carrega a
  imagem, roda smoke e confere labels antes de dar push.
- `container.yml` builda um wheel do fonte atual com o ambiente de build e
  `umask 022`, constrói `Dockerfile.ci` a partir desse wheel e roda o smoke;
  o caminho de PR fica verde antes de 0.3.0 existir no PyPI.
- 198 testes.

## [0.2.0] - 2026-11-26

### Breaking

- **Chaves de topo desconhecidas agora reprovam.** O loader reconhece um
  conjunto fixo de chaves por versão de config e lista todas as desconhecidas
  em `atesto.json`/`pstack.json`, em todos os modos (`check`, `check --ci`,
  `proof`, `review`, `doctor`). Não há modo de aviso nem escape: a config que
  parece mais forte não pode ser aplicada só em parte.
- **Migração antes de atualizar:** mova chaves de extensão/domínio — por
  exemplo `statechart`, `blast_radius`, decisões ou requisitos por path — para
  a config do adaptador. O core não as reconhece; mantê-las na config do
  Atesto reprova. `check --ci` também rejeita chaves desconhecidas e downgrade
  de `version` da base.

### Added

- Config v2 com `test_timeout_seconds` (inteiro >= 1; ausente = sem timeout).
  `init` passa a escrever v2; configs v1 sem chaves novas continuam válidas.
- Timeout do `test_cmd`: o comando roda em grupo de processos próprio, recebe
  SIGTERM no grupo, tem uma carência curta e então SIGKILL. A falha é fechada,
  nomeia o limite e o comando, e `check --json` expõe
  `test_timeout_seconds` e `timed_out`. Em plataformas sem grupos de processo,
  só o filho direto é encerrado (limitação documentada).
- `tool: {"name": "atesto", "version": "0.2.0"}` em `proof.json` e
  `review.json`; o `version` é validado como semver-like (aceita prerelease e
  build metadata, como `0.3.0rc1` e `0.3.0-dev.1`). Exposto em `check --json`
  e no `doctor`. Artefatos antigos sem `tool` continuam válidos.
- `doctor`: lê a config crua sem quebrar e reporta chaves desconhecidas,
  versão, chaves efetivas (`test_cmd`, `review_artifacts_paths`, `excludes`
  etc.), timeout, raiz de artefatos, frescor dos artefatos, hook e versão da
  ferramenta; sai com código != 0 quando há problema, inclusive quando a
  evidência exigida está ausente ou a raiz de artefatos está vazia. Hook
  ausente é informativo.
- JSON Schema draft 2020-12 para v1 e v2, com `additionalProperties: false`,
  mantido em sincronia com o loader por teste.

### Changed

- Template de CI pinado em `atesto==0.2.0` com sha256, Python 3.12,
  `--require-hashes --no-deps` e smoke `atesto --version` antes do gate.
- Wheel reproduzível: `SOURCE_DATE_EPOCH` fixo e ambiente de build do release
  pinado no workflow (`build==1.6.1`, `setuptools==84.2.1`, `wheel==0.48.0`,
  `python -m build --no-isolation`); `[build-system]` segue permissivo
  (`setuptools>=69`) para builds a partir do fonte. O hash do template é o
  sha256 do wheel publicado; rebuilds locais exigem checkout limpo com
  `umask 022` (permissões de arquivo entram no zip).
- 113 testes.

## [0.1.1] - 2026-11-26

- Review fail-closed: um `review.json` existente em qualquer artefato candidato
  é validado; review inválida não é escondida por outro artefato.
- `test_cmd` compara o delta de arquivos rastreados e aceita árvore já suja;
  caches untracked não reprovam.
- Symlink para diretório (inclusive fora do repo) volta a entrar no tree hash.
- `--ci` usa a árvore do HEAD, não o worktree sujo.
- Múltiplos artefatos preferem prova com review válida quando review é exigida.
- Endurecimento: reviewers únicos, `schema` booleano rejeitado, excludes
  normalizados, `require` sempre com proof, root obrigatório na raiz do repo.
- 61 testes.

## [0.1.0] - 2026-11-26

- Primeiro release: proof/review/check ligados ao tree hash.
- Config `atesto.json`, fallback `pstack.json`.
- Hook e template de CI opcionais em `integrations/`.
