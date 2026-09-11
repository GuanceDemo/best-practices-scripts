# OCI WAF 日志接入观测云

本示例通过 OCI Object Storage 对象创建事件触发 OCI Function。Function 使用 Resource Principal 读取新对象，并将日志发送到观测云 Dataway 或 DataKit。

```text
OCI WAF -> Object Storage -> OCI Events -> OCI Function -> Dataway/DataKit -> 观测云
```

OCI Events 传递的是对象元数据，不包含对象正文。因此 Function 收到事件后仍需调用 Object Storage `GetObject`。

## 目录说明

| 文件 | 说明 |
| --- | --- |
| `func.py` | OCI Function 入口及 Resource Principal 初始化 |
| `forwarder.py` | 获取对象、逐行读取日志并发送到观测云 |
| `config.py` | 环境变量配置与校验 |
| `dataway.py` / `datakit.py` | Dataway、DataKit HTTP 写入客户端 |
| `oci/` | 为减小镜像体积而裁剪的 OCI Python SDK |
| `Dockerfile.optimized` | OCI Function 构建文件 |
| `func.yaml` | Function 定义及非敏感默认配置 |

## 前置条件

- OCI WAF 已将日志投递到同一区域的 Object Storage Bucket。
- 已创建 OCI Functions Application，且网络可以访问 Dataway 或 DataKit。
- 已安装并配置 OCI CLI、Fn CLI 和 Docker。
- 观测云中已创建目标日志索引。

## IAM 策略

将下列占位符替换为实际值。部署账号所属用户组和 Function 动态组是两个不同主体。

```text
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to manage functions-family in compartment <COMPARTMENT_NAME>
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to manage repos in compartment <COMPARTMENT_NAME>
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to manage cloudevents-rules in compartment <COMPARTMENT_NAME>
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to use virtual-network-family in compartment <COMPARTMENT_NAME>
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to inspect compartments in tenancy

Allow dynamic-group <FUNCTION_DYNAMIC_GROUP> to read objects in compartment <COMPARTMENT_NAME> where target.bucket.name = '<BUCKET_NAME>'
Allow service cloudEvents to use functions-family in compartment <COMPARTMENT_NAME>
```

如使用 OCI Vault Secret 保存 Dataway Token，还需允许动态组读取对应 Secret。

## 配置

必须选择一种输出方式：

### 直接写入 Dataway

```bash
fn config function <APP_NAME> <FUNCTION_NAME> DATAWAY_URL 'https://<DATAWAY_HOST>'
fn config function <APP_NAME> <FUNCTION_NAME> DATAWAY_TOKEN '<WORKSPACE_TOKEN>'
fn config function <APP_NAME> <FUNCTION_NAME> STORAGE_INDEX '<LOG_INDEX>'
fn config function <APP_NAME> <FUNCTION_NAME> ALLOWED_BUCKET '<BUCKET_NAME>'
fn config function <APP_NAME> <FUNCTION_NAME> OCI_REGION '<OCI_REGION>'
fn config function <APP_NAME> <FUNCTION_NAME> ENV '<ENVIRONMENT>'
```

生产环境建议使用 `DATAWAY_TOKEN_SECRET_OCID` 从 OCI Vault Secret 读取 Token，避免将 Token 直接写入 Function 配置。

### 写入 DataKit

```bash
fn config function <APP_NAME> <FUNCTION_NAME> DATAKIT_IP '<DATAKIT_IP_OR_HOST>'
fn config function <APP_NAME> <FUNCTION_NAME> DATAKIT_PORT '9529'
fn config function <APP_NAME> <FUNCTION_NAME> DATAKIT_PROTOCOL 'http'
fn config function <APP_NAME> <FUNCTION_NAME> STORAGE_INDEX '<LOG_INDEX>'
fn config function <APP_NAME> <FUNCTION_NAME> ALLOWED_BUCKET '<BUCKET_NAME>'
```

可选配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SOURCE` | `oci_waf` | 日志 source/measurement |
| `SERVICE` | `oci_waf` | 日志 service 标签 |
| `OBJECT_PREFIX` | 空 | 只接受指定对象名前缀 |
| `TAGS` | 空 | 自定义标签，格式为 `key:value,key2:value2` |
| `LOG_SAMPLE_RATE` | `1` | 稳定采样率，范围 `(0, 1]` |
| `HTTP_TIMEOUT` | `15` | HTTP 超时秒数 |
| `MAX_RETRIES` | `3` | 请求失败最大重试次数 |

## 构建与部署

配置 Fn Context 后，在本目录执行：

```bash
fn deploy --app <APP_NAME> --verbose
```

`Dockerfile.optimized` 会安装 FDK，并使用仓库中的裁剪版 OCI SDK。不要同时安装完整 `oci` PyPI 包，否则镜像体积会显著增加。

## Events Rule

创建 Event Rule，匹配 Object Storage 的对象创建事件，并将 Action 指向本 Function：

```json
{
  "eventType": "com.oraclecloud.objectstorage.createobject",
  "data": {
    "resourceName": "waf-logs/example.json.gz",
    "additionalDetails": {
      "namespace": "<TENANCY_NAMESPACE>",
      "bucketName": "<BUCKET_NAME>"
    }
  }
}
```

建议在规则条件中同时限制 Bucket 名称和对象前缀，并在 Function 中配置 `ALLOWED_BUCKET`、`OBJECT_PREFIX` 做二次校验。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

上传一个不含敏感信息的测试日志对象后，依次确认：

1. Event Rule 指标中出现匹配和 Action 执行。
2. Function Invocation 日志中出现 `OCI WAF object completed`。
3. 观测云目标索引中出现 `source=oci_waf` 的日志。

## 常见问题

### 创建 Event Rule 返回 `NotAuthorizedOrNotFound`

即使已有 `manage cloudevents-rules`，Events 服务在创建规则时仍可能需要解析目标 Compartment。为部署组增加以下策略：

```text
Allow group '<IDENTITY_DOMAIN>'/'<DEPLOYER_GROUP>' to inspect compartments in tenancy
```

同时确认 CLI 使用的 Region、Compartment OCID、Identity Domain 和 Group 名称正确，并等待 IAM 策略传播后重试。

### Function 无法读取对象

确认 Function 已被动态组规则匹配，且动态组具有 Bucket 的 `read objects` 权限。事件触发成功不代表 Function 自动具备对象读取权限。

### Dataway 没有收到日志

确认 Application/Function 配置中的 Dataway 地址、Token、目标索引和网络连通性。不要在日志或代码仓库中输出 Token。

## 安全说明

- 不要提交 OCI API 私钥、Auth Token、Dataway Token、真实 OCID 或客户日志。
- 建议通过 OCI Vault Secret 管理 Dataway Token。
- 使用 `ALLOWED_BUCKET` 和 `OBJECT_PREFIX` 限制 Function 可处理的对象范围。
- 测试事件和日志必须使用脱敏数据。
