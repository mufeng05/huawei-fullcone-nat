# Huawei NetEngine AR 系列动态 IP 全锥 NAT 自动更新

PPPoE 拨号场景下，通过 OPS 维护助手脚本自动跟随 Dialer1 公网 IP 变化，重写 NAT 地址池的 `section`，实现稳定的全锥型（Full-Cone）NAT。

## 适用设备

- **NetEngine AR5700 / AR6700 / AR8000**
- 软件版本 **V600R025C10**、**V600R026C00SPC100**（其他相近版本应该兼容，CLI 语法可能微调）

测试机型：AR5710-H4T4N2X。

## 解决什么问题

华为 AR 系列支持 `mode full-cone global`（教科书定义的全锥 NAT，华为术语"三元组 NAT"），但 NAT 地址池的 `section` 命令必须填具体 IP，**不支持引用接口 IP 或动态 IP**。在 PPPoE 拨号每次拿到不同公网 IP 的场景下，没有原生机制让 section 跟着 Dialer1 走。

本项目用 OPS（Open Programmability System，华为 VRP 内置的 Python 自动化框架）维护助手解决这个问题：

- **事件触发**：订阅 `RM/RM_ADD_DEFAULTRT` 事件，PPPoE 上线后秒级响应，读取 Dialer1 接口的新 IP，重写 section
- **定时兜底**：每 60 秒用两条只读 `display` 比对 Dialer1 当前 IP 与 section，事件没触发也能补救，平时不进配置模式

两个脚本独立部署，比对当前 section 后才写，写完回读校验，幂等无副作用。

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

### 一条经过 Dialer1 的默认路由

`update_pool.py` 依赖事件 `RM_ADD_DEFAULTRT`（过滤 `Ifname=Dialer1`），需要有一条走 Dialer1 的静态默认路由，例如：

```
ip route-static 0.0.0.0 0 Dialer1
```

带不带下一跳都可以：脚本只把这个事件当触发器，新 IP 直接读 Dialer1 接口（`display ip interface brief Dialer1`），不依赖事件参数 `_para_Nexthop`。

> 旧版脚本把 `_para_Nexthop` 当作新 IP，要求默认路由不能带下一跳（不带时 VRP 会把 Dialer1 自己的 IP 填进 Nexthop 字段）；显式写了 `ip route-static 0 0 Dialer1 <peer-ip>` 的话会把 PPP 对端 IP 写进地址池。现在已经没有这个限制。

### NAT 配置

```
# 地址池（IP 占位，脚本会改）
# exclude-port：排除路由器自己在公网上监听、且 >= 2048 的端口（这里是 IPsec NAT-T 4500、
# Web 管理 8443），以及 NAT Server 转发出去的端口，见下文"端口转发"和"已知限制"
nat address-group p2p_pool
 mode full-cone global
 route enable
 section 0 1.1.1.1 1.1.1.1
 exclude-port 4500
 exclude-port 8443

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
# （缺省就是开启的，可以用 display firewall endpoint-independent filter 确认）
firewall endpoint-independent filter enable
```

地址池名 `p2p_pool`、section 编号 `0`、网段 `192.168.0.0/24` 改成你的实际值，脚本里对应的常量也要同步改。

### 端口转发（NAT Server）要加 `no-reverse`

如果还要给内网主机做端口转发，`nat server` 必须带 `no-reverse`：

```
nat server debian-ssh protocol tcp global interface Dialer1 52200 inside 192.168.0.110 22 no-reverse
```

不带 `no-reverse` 时，设备会额外生成一条反向 Server-map（`display firewall server-map` 里的 `Nat Server Reverse, 192.168.0.110[公网IP] -> ANY`）。它只按"内网 IP + 协议"匹配、不看端口，而且优先于 NAT 策略：这台主机该协议的**所有**出站连接都会被它只换 IP、不换端口地转出去，不再走 `allow_fullcone`，也就不会生成三元组表项，于是这台主机退化成端口受限锥（NAT3）。反向表项按协议分别生成，所以只配了 TCP 转发时只有 TCP 退化，UDP 仍是全锥。

实测：带反向表项时，主机出站的公网端口始终等于本地端口，RFC 5780 换 IP / 换端口的回包全部收不到；加 `no-reverse` 后恢复 EIM + EIF，转发端口从公网照常能连进来。

注意：

- 已有的 `nat server` 不能原地修改（会报 `The name of the NAT server already exists`），要先 `undo nat server name <名字>` 再带 `no-reverse` 重新添加，期间经过这些端口的已有连接会断开
- 建议同一台主机的 NAT Server 全部带上 `no-reverse`：反向表项按"主机 + 协议"生成，漏一条就可能还在
- 转发出去的公网端口要加进地址池的 `exclude-port`，避免和三元组分配的端口冲突
- 连续端口可以合成一条：`global interface Dialer1 11010 11012 inside 192.168.0.110 11010 11012 no-reverse`
- 检查：`display firewall server-map | include Reverse` 没有输出才对

## 脚本说明

### `update_pool.py`（主脚本，事件触发）

订阅 `RM / RM_ADD_DEFAULTRT` 事件，过滤 `Ifname=Dialer1, AfType=IPv4`。PPPoE 上线触发默认路由添加 → 读 Dialer1 接口当前 IP（`display ip interface brief Dialer1`）和地址池当前 section（`display nat address-group name p2p_pool`）→ 不一致才进配置模式 `undo section` + `section` → 回读确认。

每次触发打一条 `update_pool start: ...`（事件里的下一跳、Dialer1 IP、当前 section），写入后再打一条 `done` 或 `FAILED`。

### `update_pool_polling.py`（兜底，定时轮询）

`timer.relative("tick", 60)` 每 60 秒触发一次，走与主脚本相同的比对 + 更新流程。比对只用两条用户视图的 `display`，平时不进配置模式。

静默运行，**只在真正写入后**才打一条 syslog：`update_poll done: ...` 或 `update_poll FAILED: ...`，避免日志刷屏。

两个脚本的写入都会检查每条命令的返回状态，写完再读一次 section 确认；失败时日志里会写明是哪条命令失败（或者回读不一致），下一次触发会自动重试。

实测（V600R026C00SPC100）：

- IP 没变时，新的轮询每分钟往日志文件里写 8 行（6 行 OPS RESTCONF 记录 + 2 行命令记录），旧版是 12 行，而且旧版每分钟都会进一次 `system-view` 和地址池视图
- 一次执行的耗时主要是 OPS 自身的开销：定时器触发后要 6 秒左右才执行到第一条命令，命令本身 1～2 秒，所以少几条命令对耗时影响不大
- 平时 PPPoE 重拨时，事件脚本 3～6 秒内完成比对和更新（历史日志里三次重拨的数据）

### 脚本日志在哪看

`_ops.syslog()` 写出来的是 `OPS/6/OPS_LOG_USERDEFINED_INFORMATION`（informational 级别）。logbuffer 通道缺省只记录 warning 及以上（`display info-center channel 4` 可以看到 `default ... warning`），所以**脚本日志不在 `display logbuffer` 里**，只写进 flash 上的日志文件：

```
more logfile/log.log | include update_pool
```

日志文件轮转后，旧内容在 `logfile/log_*.log.zip` 里，可以用 SFTP 下载后解压搜索。不建议为了看脚本日志把 OPS 模块的 logbuffer 级别调到 informational（`info-center source OPS channel logbuffer log level informational`）：OPS 每执行一条命令还会产生 notification 级别的 RESTCONF 记录，会一起涌进 logbuffer。

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
more logfile/log.log | include update_pool
```

应该看到两行：
```
update_pool start: route nexthop='NEW_IP', Dialer1='NEW_IP', section='OLD_IP'
update_pool done: Dialer1 OLD_IP -> NEW_IP
```

如果重拨后 IP 没变，只会有 `start` 一行，正常。

轮询脚本：等 60 秒以上，如果 IP 没变化不打日志，正常。手工改个错误 section 看下次轮询会不会自愈（测试期间全锥出口用的是错误 IP，内网最长会断网 60 秒）：

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

### 不影响生产的验证方法

上面两种验证都会让内网短暂断网。如果不方便（比如人不在路由器旁边），可以用环回口 + 临时地址池验证脚本本身，全程不碰 Dialer1、`p2p_pool` 和 NAT 策略：

1. 建一个环回口和一个不被任何 NAT 策略引用的临时地址池，地址用文档专用网段（RFC 5737），section 先故意写错：
   ```
   system-view
   interface LoopBack100
    ip address 192.0.2.1 255.255.255.255
    quit
   nat address-group zz_test_pool1
    section 0 192.0.2.9 192.0.2.9
    return
   ```
2. 复制一份脚本改名（比如 `zz_poll_test.py`），把常量改成 `POOL_NAME = "zz_test_pool1"`、`DIALER = "LoopBack100"`，日志前缀也改掉以便区分，按上面的方法上传、安装、注册。测事件脚本时可以临时把 `ops_condition` 换成 `timer.relative`，不用重拨
3. 一分钟内 section 应该变成 192.0.2.1；再把环回口地址改成 192.0.2.2，模拟换 IP，下一分钟 section 应该跟着变
4. 测写入失败：再建一个临时池占住某个地址，然后把环回口改成这个地址。不同地址池的 section 不能重叠（会报 `A NAT address section conflict occurs`），脚本应该打出 `FAILED ... failed at section ... (Error: Failed to execute the command.)`
5. 清理：`undo script-assistant python ...`、`ops uninstall file ...`、`undo nat address-group ...`、`undo interface LoopBack100`，再用 SFTP 删掉上传的文件

注意：如果开了 `configuration file auto-save`，测试期间的中间状态可能已经被自动保存进启动配置。清理完先确认 `display saved-configuration` 里没有测试残留，有的话执行一次 `save`。

## 验证全锥 NAT 语义

光改 section 不够，还要确认行为真的是全锥。在内网 PC 上跑 [NatTypeTester](https://github.com/HMBSbige/NatTypeTester)（Windows）或 `stunclient`（Linux/Mac）。

期望结果：**Full Cone NAT**。

如果测出 Restricted Cone / Port Restricted Cone / Symmetric，排查方向：

1. `firewall endpoint-independent filter enable` 是否真的开着（`display current-configuration | include endpoint-independent`）
2. NAT 地址池 `mode full-cone global` 是否真生效（`display nat address-group`）
3. NAT 策略命中的是不是 `allow_fullcone` 这条（`display nat-policy rule name allow_fullcone`，看 hits）
4. ASPF 等应用层协议检测是否对相关协议有干扰
5. 这台主机有没有不带 `no-reverse` 的端口转发（`display firewall server-map | include Reverse` 有输出就是有，见上文"端口转发"）。典型现象：只有配了转发的那台机器、只有配了转发的那个协议退化成 Port Restricted Cone，而且公网端口和本地端口一样

## 可选调优

```
# 三元组映射的空闲老化时间，默认 60 秒；RFC 4787 要求 UDP 映射空闲不少于 2 分钟，推荐 5 分钟
firewall server-map aging-time full-cone 300
```

老化调长后，空闲映射会多保留一会儿，端口占用会上升（实测一个家庭网络从约 3% 升到约 7%），可以用 `display nat resource usage address-group name p2p_pool` 观察。每条三元组映射占 1 个公网端口，在 `display firewall server-map` 里对应 `FullCone Src` + `FullCone Dst` 两条表项。

## 已知限制

### 路由器自己在公网上的服务端口要排除

华为 AR 系列文档明确：

> 配置 NAT No-PAT、三元组 NAT 这两种源 NAT 时，请不要将设备接口的地址配置为 NAT 地址池的地址。

PPPoE 单公网 IP 场景，地址池 IP 就是 Dialer1 IP，无法回避。原因是从公网到该 IP 的入向报文会优先匹配三元组生成的 Server-map：如果某个端口恰好被分配给了内网主机，发往这个端口的连接会被转给那台主机，而不是路由器自己。

实际影响没有"公网完全无法管理"那么大：三元组地址池只分配 2048～65535 的端口（实测在用的最低 2048、最高 65474），22（SSH）、80、500（IKE）这些 2048 以下的端口不会被占用，实测从公网 SSH / Web 管理都能正常连上。会被间歇抢走的是路由器监听在 2048 以上的服务，比如 IPsec NAT-T 的 4500、Web 管理的 8443、改到高位端口的 SSH，把它们加进地址池的 `exclude-port` 即可（见上文"NAT 配置"）。

路由器在监听哪些端口可以用 `display tcp status` / `display udp status` 查看。

### 重启后有几分钟空窗

开机时 PPPoE 往往比 OPS 维护助手先就绪。实测一次重启：设备 13:04:10 启动，13:04:37 PPPoE 拨上并产生 `RM_ADD_DEFAULTRT`，但两个维护助手到 13:10:45～13:11:31 才加载完成，**这次事件被错过了**，主脚本没有执行。

如果重启后 PPPoE 换了 IP，地址池在这段时间里还是启动配置里的旧 IP，走 `allow_fullcone` 的流量会用旧 IP 出去，内网相当于断网，要等 OPS 就绪后由轮询脚本修正（开机后约 7～8 分钟）。着急的话可以手工执行 `undo section 0` + `section 0 <新IP> <新IP>`。

平时只重拨、不重启时不受影响：OPS 已经在运行，事件脚本 3～6 秒内完成更新（见"脚本说明"里的实测）。

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

看脚本日志（在日志文件里，不在 logbuffer 里，见"脚本日志在哪看"）：
```
more logfile/log.log | include update_pool|update_poll
```

- `update_pool start: ... Dialer1=None` → 没读到 Dialer1 的 IP（接口名不对，或者 PPPoE 还没拿到地址）
- `... FAILED: ..., failed at <命令> (<desc>)` → 这条命令执行失败
- `... FAILED: ..., failed at verify` → 命令都返回成功，但回读的 section 跟预期不一致

常见原因：
- `set_model_type` 没调，CLI 子视图命令全失败
- `nat address-group` 名字或 section 编号不对
- 新 IP 和别的地址池的 section 重叠（`A NAT address section conflict occurs`，日志里是 `failed at section ... (Error: Failed to execute the command.)`）

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

以下内容参考 `NetEngine AR5700, AR6700, AR8000 V600R026C00 产品文档.chm`：

- 端口转发与反向 Server-map：`security_nat_cfg_0031.html`（NAT Server 原理："如果命令中不配置 no-reverse 参数，设备还将自动生成反向 Server-Map"）、命令参考 `nat server(系统视图)`
- 三元组老化时间：命令参考 `firewall server-map aging-time full-cone`
- 端点无关过滤：命令参考 `firewall endpoint-independent filter enable`（"该命令只对 NAT 三元组和 NAT64 三元组生效"）

## License

MIT
