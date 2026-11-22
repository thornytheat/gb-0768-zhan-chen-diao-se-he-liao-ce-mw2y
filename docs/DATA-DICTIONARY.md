# 工坊收据数据定义

| 文件 | 字段 |
|---|---|
| job.json | `order` 工单字符串；`start,end,cutoff` 为带偏移 ISO 时间，`start < end ≤ cutoff`，工单最多 31 天 |
| weights.csv | `ticket,pot,container,action,lot,gross,unit,occurred,received`。票号前导零保留；`pot` 是混料账，`container` 决定皮重；动作 `OPEN,ISSUE,APPLY,LOSS,RETURN`；入罐需颜料别名，出罐的 `lot` 留空；毛重含容器；收件不早于事件 |
| tares.csv | `container,version,start,end,mass,unit`。皮重版本按称量发生时刻在半开区间匹配；重叠的所有原行都进入争议证据 |
| lots.csv | `alias,lot,pigment`，别名按 NFKC、去边空白和大小写折叠关联；规范批号为登记原值 |
| claims.csv | `lot,mass,unit,receipt`。每规范批号的回桶总量，不同值视为冲突，相同值保留全部来源 |

重量以 Decimal 转为整数毫克，支持 kg/g/mg、合法千分位与全角。负数、超过 10ⁱ⁰ mg 或小于整数毫克精度的重量隔离。`NULL`、`N/A`、`-`、`未知` 和空串是缺值，实际零保留。输出以 `_mg` 命名整数毫克，界面显示克至三位小数。时刻统一 UTC，页面为 UTC+08:00。工单、皮重区间均为 `[start,end)`，收件截止时刻可含端点。

表单为 UTF-8 CSV，支持 BOM 及换列；逻辑行保留原件起始物理行。空文件、仅表头和无可读行分开表达。重复表头计入独立项，坏列保留原值。单表限制 150,000 字符，整个请求 5 MiB。

同一工单内，`ticket` 是全表业务键。截止前同号内容互相矛盾时整组隔离，相同则取最早收件及最低行号。超过 1 小时的迟补记录保留，截止后排除。按实际发生时刻回放，同刻先期初、领用，再上件、损耗、回桶；同种动作按票号排序。

上件、损耗和回桶按该时点的罐内各批号实际存量比例摊分。计算整数毫克商及余数，按余数降序、规范批号升序补齐至实称净重。超用隔离且保持现存量。期末未完成回桶的罐内存量为留罐争议。坏秤单不凭推测增加供给方重量。

## 皮重更正

`POST /api/books/{key}/corrections` 受理已封存工单的皮重更正，不改写旧留档。

```json
{"tare":[{"container":"TIN-A","version":"TA1","mass":"22","unit":"g",
          "start":"2026-11-08T08:00:00+08:00","end":"2026-11-08T18:00:00+08:00"}],
 "reason":"补测换罐"}
```

- `tare` 可为对象或数组。定位到 `container + version` 的原行时改写该行；定位不到且给出 `start/end` 时作为新版本追加。
- 只改区间时可省略 `mass/unit`；只改皮重时可省略 `start/end`。`unit` 缺省沿用原行或按 `g` 校验。
- 同容器同版本的生效区间重叠会被拒绝。一次更正中同容器同版本不得重复。
- 修订键取父留档键与规范化后的 `container/version/start/end/tare_mg`（时间转 UTC、重量转整数毫克），故同一更正重试恒得同键、不重复签发。原因文字不进键。
- 更正收据登记 `manifest.revision`、`manifest.root`、`manifest.correction.parent`、`changes`（原行与更正后行）以及 `affected`（受影响票的净重、成分分摊、存量前后值与总量）。
- 重算失败（原已计价票失去皮重、出现新回桶隔离、成分失衡或异常）返回 `422 CORRECTION_UNSTABLE`，不落盘，旧版照常可查可下载。
- `GET /api/books/{key}/revisions` 返回同一根单的修订链；`POST /api/books/{key}/corrections/preview` 只预览不签发。
