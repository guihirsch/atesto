# Minimal example

Run this in a throwaway git repo:

```bash
git init
git config user.email you@example.com
git config user.name You
echo 'x = 1' > app.py
git add app.py && git commit -m base

atesto init --require proof
atesto proof demo -- python3 -c "print('ok')"
git add . && git commit -m evidence
atesto check
```

Expected: `atesto: ok`.

For review:

```bash
atesto init --require proof,review --force
atesto proof demo -- python3 -c "print('ok')"
git add . && git commit -m evidence
atesto review demo --head HEAD --verdict APPROVED --reviewer you
git add . && git commit -m review
atesto check
```
