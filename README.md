# 多站卫星地面站排程系统

使用标准库与 SQLite 实现的独立排程原型。系统维护卫星、地面站、天线、维护时段、可见窗口、租户配额和数据请求，并检查速率、数据量、截止时间、设备重叠、卫星同时接收、天气和租户配额。

## 运行

```bash
python3 app.py --db satellite_scheduling.db
```

默认监听 `127.0.0.1:8204`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；`requester` 还需 `X-Tenant`。角色：`viewer`、`requester`、`operator`、`commander`、`auditor`。

## 主要接口

- `POST /api/satellites`、`/api/stations`、`/api/antennas`、`/api/maintenance`、`/api/visibility-windows`、`/api/quotas`：资源配置。
- `POST /api/requests`：创建数据接收请求。
- `POST /api/requests/{id}/schedule`、`/reschedule`：排程或重排被抢占请求。
- `POST /api/schedules/{id}/start`、`/complete`、`/cancel`、`/preempt`：接收状态和紧急抢占。
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；已接收数据保留。
- `POST /api/receipts`：登记接收回执（实际起止时刻与接收量），同一排程重复回执只结算一次；窗口版次不一致挂起待核对，超出可见窗口整条拒绝，少收部分退回租户当天配额。
- `POST /api/receipts/{id}/retry`：重试待处理回执的结算，已结算幂等返回、不重复入账。
- `GET /api/receipts`、`GET /api/receipts/{id}`：回执入口查询。
- `GET /api/settlements`、`GET /api/settlements/{id}`：结算账查询。
- `GET /api/reconciliation`：对账台（待处理/挂起/拒绝/待回填汇总与结算总量）。
- `POST /api/reconciliation/backfill`：指挥官升级回填历史无回执排程，按计划值结算。
- `GET /api/quotas/usage?tenant=&station_id=&day=`：租户当日配额用量查询。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
