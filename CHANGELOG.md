# Changelog

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
  (`setuptools>=69`) para builds a partir do fonte. O hash do wheel no
  template é verificado por build duplo.
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
