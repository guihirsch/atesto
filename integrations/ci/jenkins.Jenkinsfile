// Atesto gate - optional, additive Jenkins stage.
//
// Add this stage to a pipeline (or copy it into a new Jenkinsfile); do not
// replace existing stages. The image version must match pyproject.toml; the
// structural test `CISnippetTest` fails when it drifts.
//
// BASE_REF is CHANGE_TARGET, the pull request target branch. Merge
// enforcement stays with your Jenkins branch protection or SCM rules, not
// with this stage.
//
// Generate docs/atesto/<slug>/review.json before you commit the branch (see
// README.md); this stage only runs the gate. `check --ci` requires the
// evidence directory to be part of the PR diff, so CI must not create it.
pipeline {
  agent any

  stages {
    stage('Checkout (full history)') {
      when { changeRequest() }
      steps {
        checkout([
          $class: 'GitSCM',
          branches: scm.branches,
          extensions: [[
            $class: 'CloneOption',
            depth: 0,
            shallow: false,
            noTags: false
          ]],
          userRemoteConfigs: scm.userRemoteConfigs
        ])
        sh '''
          set -eu
          git fetch origin \
            "+refs/heads/${CHANGE_TARGET}:refs/remotes/origin/${CHANGE_TARGET}"
        '''
      }
    }

    stage('Atesto gate') {
      when { changeRequest() }
      steps {
        sh '''
          set -eu
          BASE_REF="origin/${CHANGE_TARGET}"
          # Match the Jenkins user so the bind-mounted .git is trusted and
          # writable; the image itself runs as non-root.
          docker run --rm --user "$(id -u):$(id -g)" \
            -v "$WORKSPACE:/repo" -w /repo \
            ghcr.io/guihirsch/atesto:0.2.0 \
            check --ci --base "$BASE_REF"
        '''
      }
    }
  }
}
