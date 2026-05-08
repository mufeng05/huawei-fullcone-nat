# Huawei NetEngine AR 系列动态 IP 全锥 NAT 自动更新

PPPoE 拨号场景下，通过 OPS 维护助手脚本自动跟随 Dialer1 公网 IP 变化，重写 NAT 地址池的 `section`，实现稳定的全锥型（Full-Cone）NAT。

## 适用设备

- **NetEngine AR5700 / AR6700 / AR8000**
- 软件版本 **V600R025C10**（其他相近版本应该兼容，CLI 语法可能微调）

测试机型：AR5710-H4T4N2X。

## 解决什么问题

华为 AR 系列支持 `mode full-cone global`（教科书定义的全锥 NAT，华为术语"三元组 NAT"），但 NAT 地址池的 `section` 命令必须填具体 IP，**不支持引用接口 IP 或动态 IP**。在 PPPoE 拨号每次拿到不同公网 IP 的场景下，没有原生机制让 section 跟着 Dialer1 走。

本项目用 OPS（Open Programmability System，华为 VRP 内置的 Python 自动化框架）维护助手解决这个问题：

- **事件触发**：订阅 `RM/RM_ADD_DEFAULTRT` 事件，PPPoE 上线后秒级响应，从事件参数 `_para_Nexthop` 直接拿到 Dialer1 新 IP，重写 section
- **定时兜底**：每 60 秒查询一次 Dialer1 当前 IP 与 section 比对，事件没触发也能补救

两个脚本独立部署，比对当前 section 后才写，幂等无副作用。

## 前置条件

### 拓扑

光猫桥接 + AR 路由器 PPPoE 拨号，Dialer1 直接拿公网 IP：

```
Internet
   │
   ↓ 光猫桥接
[AR Router]  ← Dialer1 拨号
   │
   ↓ LAN
内网 PC
```

如果你前置另一台路由器做 NAT，最终 NAT 类型取决于前置设备，本方案不适用。

### 一条已存在的静态默认路由

`update_pool.py` 依赖事件 `RM_ADD_DEFAULTRT`，前提是路由配置成：

```
ip route-static 0.0.0.0 0 Dialer1
```

**不要带 nexthop 参数**。VRP 在这种写法下会把 Dialer1 自己的 IP 填进路由表的 Nexthop 字段（不是 PPP 对端 IP），事件参数 `_para_Nexthop` 才能直接当作新公网 IP 使用。

如果你的默认路由是 `ip route-static 0 0 Dialer1 <peer-ip>`（显式指定下一跳），主脚本拿到的会是对端 IP 不是自己的 IP，要改写脚本去查询 Dialer1 接口。

### NAT 配置

```
# 地址池（IP 占位，脚本会改）
nat address-group p2p_pool
 mode full-cone global
 route enable
 section 0 1.1.1.1 1.1.1.1

# NAT 策略（必须指定 egress-interface Dialer1，否则会污染其他出接口的流量）
nat-policy
 rule name allow_fullcone
  egress-interface Dialer1
  source-address 192.168.0.0 mask 255.255.255.0
  action source-nat address-group p2p_pool

# Dialer1 接口下开启 NAT
interface Dialer1
 nat enable

# 关键：开启端点无关过滤，否则全锥语义不完整退化为受限锥
firewall endpoint-independent filter enable
```

地址池名 `p2p_pool`、section 编号 `0`、网段 `192.168.0.0/24` 改成你的实际值，脚本里对应的常量也要同步改。

## 脚本说明

### `update_pool.py`（主脚本，事件触发）

订阅 `RM / RM_ADD_DEFAULTRT` 事件，过滤 `Ifname=Dialer1, AfType=IPv4`。PPPoE 上线触发默认路由添加 → 脚本拿 `_para_Nexthop` → 进入 `nat address-group p2p_pool` 视图 → `display this` 解析当前 section → 不一致才 `undo section` + `section`。

每步命令带 syslog 调试输出，便于诊断。

### `update_pool_polling.py`（兜底，定时轮询）

`timer.relative("tick", 60)` 每 60 秒触发一次。`display ip interface brief Dialer1` 取当前 IP，再走与主脚本相同的比对+更新流程。

静默运行，**只在真正写入新 section 后**才打一条 syslog `update_poll done: ...`，避免日志刷屏。

### 为什么分两个脚本不合并

OPS 的 `correlate("rtAdd or tick")` 跨类型组合（event + timer）实测无效——assistant State 卡在 `init`，两个 Subscribe 都 success 但整体不进 ready。Huawei 文档"多条件关系组合"官方示例只展示同类组合（两个 cli 事件 AND），timer + event 跨类型 OR 没明确支持。所以拆开各自独立 assistant 部署。

由于双方都是 compare-then-update 幂等逻辑，并行跑不会冲突。

## 部署

### 1. 改脚本里的常量

两个脚本顶部都有：

```python
POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
```

按你实际配置改。

### 2. SFTP 上传

设备需要先开 SFTP 服务（不在本文档范围）。从你 PC 上传：

```
sftp <username>@<router-ip>
sftp> put update_pool.py
sftp> put update_pool_polling.py
sftp> bye
```

### 3. 安装并注册脚本助手

设备 SSH 进去：

```
<HomeRouter> ops install file update_pool.py
<HomeRouter> ops install file update_pool_polling.py

<HomeRouter> system-view
[HomeRouter] ops
[HomeRouter-ops] script-assistant python update_pool.py
[HomeRouter-ops] script-assistant python update_pool_polling.py
[HomeRouter-ops] return
```

### 4. 验证 assistant 状态

```
display ops assistant
display ops assistant verbose name update_pool.py
display ops assistant verbose name update_pool_polling.py
```

每个都应该看到：
- `State : ready`
- `Subscribe result : success`

如果 `State : init` 说明订阅注册有问题，检查脚本语法和事件/feature 名是否匹配。

### 5. 触发一次验证

主脚本：手动重拨 PPPoE 验证事件触发链路：

```
interface Dialer1
shutdown
undo shutdown
```

等几秒：

```
display logbuffer | include update_pool
```

应该看到完整执行轨迹，最后一行：
```
update_pool done: Dialer1 OLD_IP -> NEW_IP
```

轮询脚本：等 60 秒以上，如果 IP 没变化不打日志，正常。手工改个错误 section 看下次轮询会不会自愈：

```
system-view
nat address-group p2p_pool
undo section 0
section 0 1.1.1.1 1.1.1.1
return
```

下一次 60 秒轮询触发后应该看到：
```
update_poll done: Dialer1 1.1.1.1 -> <真实 Dialer1 IP>
```

## 验证全锥 NAT 语义

光改 section 不够，还要确认行为真的是全锥。在内网 PC 上跑 [NatTypeTester](https://github.com/HMBSbige/NatTypeTester)（Windows）或 `stunclient`（Linux/Mac）。

期望结果：**Full Cone NAT**。

如果测出 Restricted Cone / Port Restricted Cone / Symmetric，排查方向：

1. `firewall endpoint-independent filter enable` 是否真的开着（`display current-configuration | include endpoint-independent`）
2. NAT 地址池 `mode full-cone global` 是否真生效（`display nat address-group`）
3. NAT 策略命中的是不是 `allow_fullcone` 这条（`display nat-policy rule name allow_fullcone`，看 hits）
4. ASPF 等应用层协议检测是否对相关协议有干扰

## 已知限制

### 设备访问公网管理受影响

华为 AR 系列文档明确：

> 配置 NAT No-PAT、三元组 NAT 这两种源 NAT 时，请不要将设备接口的地址配置为 NAT 地址池的地址。

PPPoE 单公网 IP 场景，地址池 IP 就是 Dialer1 IP，无法回避。后果：从公网到该 IP 的入向报文会优先匹配 Server-map 表，**公网侧无法直接 SSH/Web 管理设备**，只能从内网或带外通道。

### `set_model_type("YANG")` 必须在 `cli.open()` 前调用

OPS 的隐性坑：不调用 `set_model_type` 时 CLI 通道处于半残状态，`system-view` 能进，但子视图里的命令全失败（`Error: Failed to execute the command.`）。两个脚本都已经在 `cli.open()` 前加了这一行。

### `correlate` 跨类型组合不支持

详见上面"为什么分两个脚本不合并"。

## 故障排查

### 脚本装了但不触发

```
display ops assistant verbose name <script>.py
```

看 `State`：
- `init` → 订阅没成功，可能 cron 表达式错、事件名错、feature 名错
- `ready` 但 `Running times: 0` → 订阅成功但事件没触发

排查事件是否真的产生：
```
display logbuffer | include RM_ADD_DEFAULTRT
display logbuffer | include LCPNEGOSTATE
```

### Running times > 0 但 section 没变

脚本跑了但中途 return。开 syslog 调试输出（主脚本默认开）：
```
display logbuffer | include update_pool
```

看哪一步的 `desc != 'Success'`，那条命令就是失败点。常见原因：
- `set_model_type` 没调，CLI 子视图命令全失败
- `nat address-group` 名字不对
- `_para_Nexthop` 拿不到值（事件参数命名规则随版本变化）

### 想关掉脚本

```
system-view
ops
undo script-assistant python update_pool.py
undo script-assistant python update_pool_polling.py
quit
quit
ops uninstall file update_pool.py
ops uninstall file update_pool_polling.py
```

注意：修改脚本文件内容后必须 `ops uninstall` + `ops install` 重新安装，单纯 SFTP 覆盖文件 OPS 不会重新加载。

## 文档参考

文档来源：`NetEngine AR5700, AR6700, AR8000 V600R025C10 产品文档.chm`

- 全锥 NAT 配置：`security_nat_cfg_0092.html`（三元组 NAT）、`security_nat_cfg_0016.html`（地址池）、`security_nat_cfg_0093.html`（完整配置举例）
- OPS 框架：`vrp_ops_cfg_0005.html`（维护助手）、`vrp_ops_cfg_0018.html`（OPS 能力集）
- OPS API：`vrp_ops_cfg_0034.html`（event.subscribe）、`vrp_ops_cfg_0031.html`（timer）、`vrp_ops_cfg_0040.html`（cli.execute）、`vrp_ops_cfg_0036.html`（correlate 多条件组合）、`vrp_ops_cfg_0043.html`（syslog）
- 事件参数：`v6r25c10/LOG/RM_ADD_DEFAULTRT_4.html`
- AR 平台限制：`spec/NAT_limitation_all.html`

## License

MIT
