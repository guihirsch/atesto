// Atesto gate - optional, additive Jenkins stage fragment.
//
// Paste this stage inside an existing `stages { ... }` block. It does not
// define a pipeline and does not touch existing stages. The full-pipeline
// example is `jenkins.Jenkinsfile`.
//
// BASE_REF is CHANGE_TARGET, the pull request target branch. Merge
// enforcement stays with your Jenkins branch protection or SCM rules.
//
// Generate docs/atesto/<slug>/review.json before you commit the branch (see
// README.md); this stage only runs the gate. `check --ci` requires the
// evidence directory to be part of the PR diff, so CI must not create it.
stage('Atesto gate') {
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
      BASE_REF="origin/${CHANGE_TARGET}"
      # Match the Jenkins user so the bind-mounted .git is trusted and
      # writable; the image itself runs as non-root.
      docker run --rm --user "$(id -u):$(id -g)" \
        -v "$WORKSPACE:/repo" -w /repo \
        ghcr.io/guihirsch/atesto:0.3.0 \
        check --ci --base "$BASE_REF"
    '''
  }
}
