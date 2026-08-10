# AWS CloudWatch Alarm 转观测云外部事件

该脚本运行在 DataFlux Func 中，接收 Amazon SNS 的 HTTP/HTTPS 推送，将 CloudWatch Alarm 状态变化转换为观测云“外部事件检测”格式。

## 数据链路

```text
CloudWatch Alarm → SNS Standard Topic → DataFlux Func → 观测云外部事件检测
```

## 支持能力

- 自动处理 SNS `SubscriptionConfirmation`；
- 解析 SNS `Notification` 中的 CloudWatch Alarm；
- `ALARM`、`OK`、`INSUFFICIENT_DATA` 状态映射；
- 提取账号、区域、Namespace、MetricName 和 AWS 资源维度；
- 将维度同时写入 `dimension_tags` 和事件正文；
- 限制允许接入的 Topic ARN 和 SNS 确认域名；
- 观测云写入失败时返回非 2xx，使 SNS 可以重试；
- 输出观测云 HTTP 错误正文，便于排障。

## Func 环境变量

| 环境变量 | 必填 | 示例 |
| --- | --- | --- |
| `AWS_SNS_TOPIC_ARN` | 是 | `arn:aws-cn:sns:cn-north-1:123456789012:cloudwatch-to-guance` |
| `AWS_SNS_HOST` | 是 | `sns.cn-north-1.amazonaws.com.cn` |
| `GUANCE_EXTERNAL_EVENT_URL` | 是 | `https://openapi.guance.com/api/v1/push-events/<secret>/cloudwatch` |

观测云外部事件监控器必须先保存，再从监控器详情中复制正式 Webhook。不要手工增减 URL 中的斜杠。

## 部署

1. 在 DataFlux Func 中创建脚本集和脚本。
2. 将 [`cloudwatch_to_guance.py`](./cloudwatch_to_guance.py) 的内容复制到脚本编辑器。
3. 配置三个环境变量。
4. 发布脚本 `receive` 函数。
5. 为 `receive` 创建同步 Func API，建议使用 HTTPS，超时设置为 20 秒。
6. 在 SNS Standard Topic 中创建 HTTP/HTTPS Subscription，终端节点填写 Func API 地址。
7. 等待 Func 自动完成 SNS 订阅确认。
8. 将 CloudWatch Alarm 的告警、正常和数据不足状态指向该 SNS Topic。

## 状态映射

| CloudWatch | 观测云 | `check_value` |
| --- | --- | ---: |
| `ALARM` | `error` | 1 |
| `OK` | `ok` | 0 |
| `INSUFFICIENT_DATA` | `warning` | -1 |

## 测试

可以在 SNS Topic 中发布 [`examples/sns-notification.json`](./examples/sns-notification.json) 的消息内容。Func 日志应出现：

```text
Guance external event response: 200
```

观测云事件标题应类似：

```text
[AWS CloudWatch][ALARM] CloudWatch-To-Guance-Test
```

## 生产建议

- 在当前 Topic ARN 和确认地址校验的基础上，增加完整的 AWS SNS 消息签名校验；
- 使用 `AlarmArn + NewStateValue + StateChangeTime` 作为幂等键；
- 配置 SNS 投递状态日志、重试策略和死信队列；
- 不要在日志或公开仓库中记录完整的观测云外部事件 Webhook；
- 同时配置 CloudWatch `ALARM` 与 `OK` 通知，确保异常和恢复形成闭环。

## 参考资料

- [观测云：外部事件检测](https://docs.guance.com/monitoring/monitor/third-party-event-detection/)
- [AWS：向 HTTP/HTTPS 终端节点发送 SNS 消息](https://docs.aws.amazon.com/sns/latest/dg/sns-http-https-endpoint-as-subscriber.html)
- [AWS：验证 SNS 消息签名](https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html)
