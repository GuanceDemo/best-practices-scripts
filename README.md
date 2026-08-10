# Guance Best Practice Scripts

本仓库用于存放观测云最佳实践配套的可复用脚本与示例配置。

## 脚本目录

| 目录 | 说明 |
| --- | --- |
| [`aws-cloudwatch-to-guance`](./aws-cloudwatch-to-guance/) | 通过 Amazon SNS 和 DataFlux Func 将 AWS CloudWatch Alarm 转换为观测云外部事件 |

所有脚本均使用占位符或环境变量保存账号、ARN、Webhook 等环境相关配置。使用前请阅读对应目录中的说明并完成测试。
