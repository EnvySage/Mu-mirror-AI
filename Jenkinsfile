// AI 服务流水线（Mu-mirror-AI）
//
// Python 不编译：没有构建阶段，同步代码 + 按需装依赖 + 重启 gRPC 进程。
// 重启窗口内 B 侧对 AI 掉线是"降级不炸"，用户只会短暂缺少 AI 功能，不会报错白屏。
pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    buildDiscarder(logRotator(numToKeepStr: '20'))
    timeout(time: 20, unit: 'MINUTES')
  }

  environment {
    MIRROR_HOME = '/opt/mirror'
    // 2C2G 必须用 minimal（不含 torch）；装了 torch 这台机扛不住
    REQ = 'requirements-minimal.txt'
  }

  stages {
    stage('同步代码 / 按需装依赖 / 重启') {
      steps {
        sh 'bash deploy/ai-release.sh'
      }
    }
  }

  post {
    success { echo 'AI 服务发布成功' }
    failure { echo 'AI 服务发布失败：服务可能处于旧版本或已停止，查 /opt/mirror/shared/logs/ai.err.log' }
  }
}
