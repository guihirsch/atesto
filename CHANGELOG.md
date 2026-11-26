# Changelog

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
