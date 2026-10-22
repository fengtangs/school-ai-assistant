# 华为ICT认证与考点学习手册

> 本手册整理自华技云平台内置知识库，覆盖华为 HCIA/HCIP/HCIE 备考常见知识点，供学生答疑与自学使用。

## HCIA-Datacom 考试重点（按分值）
1. **网络基础（20%）**：OSI/TCP-IP、IP编址、子网掩码、VLSM、CIDR
2. **路由基础（25%）**：静态路由、默认路由、OSPF单区域配置
3. **交换基础（20%）**：VLAN、STP/RSTP、链路聚合、端口安全
4. **广域网与安全（15%）**：NAT、ACL、IPsec、VPN
5. **网络管理（10%）**：SNMP、eSight、LLDP
6. **自动化与新兴（10%）**：SDN、Telemetry、Python运维

学习建议：用 eNSP/VRP 模拟器实操每一条命令；必做5个实验（VLAN间路由、OSPF多区域、ACL、NAT、链路聚合）；题库刷题≥800道，过线率90%+。

## OSPF 协议工作原理
OSPF（Open Shortest Path First）是基于链路状态的内部网关协议：
1. 建立邻居：Router Hello 报文发现邻居（224.0.0.5 组播）
2. 同步 LSDB：交换 LSA 形成链路状态数据库
3. 选举 DR/BDR：在 MA 网络中减少邻接关系数量
4. 运行 SPF 算法：Dijkstra 计算最短路径树
5. 维护路由表：根据开销值选路

五种报文：Hello、DD、LSR、LSU、LSAck。七种状态机：Down → Init → 2-Way → ExStart → Exchange → Loading → Full。区域类型：骨干区域（area 0）、普通区域、Stub、Totally Stub、NSSA。

## VLAN 间路由
三种方式：
1. 单臂路由（Router-on-a-Stick）：交换机Trunk连路由器，路由器起子接口封装802.1Q，成本低性能差。
2. 三层交换机 VLANIF 接口（推荐）：`interface Vlanif X` 配置IP作网关，性能高、扩展性好，企业主流。
3. SVI（交换虚拟接口）：本质同 VLANIF。

华为关键命令：
```
vlan batch 10 20
interface Vlanif10
 ip address 192.168.10.1 24
interface Vlanif20
 ip address 192.168.20.1 24
```

## VPC 与传统网络区别
VPC（Virtual Private Cloud）核心组件：子网（Subnet）、路由表（RouteTable）、安全组（SecurityGroup）、弹性IP（EIP）、对等连接/云连接。
与传统网络对比：资源逻辑隔离租户独享、几分钟开通释放、软件定义灵活拓扑、安全组+网络ACL+主机安全、按量计费、对等连接互联。

## 华为云 ECS 学习路线
- 第1周：概念与开通（ECS、镜像、VPC、安全组；控制台开通按需Linux ECS；VNC/密钥登录）
- 第2周：基础运维（systemctl、top、df -h、journalctl；安全组规则；挂载数据盘）
- 第3周：建站实战（LNMP；域名备案解析；HTTPS）
- 第4周：高阶（弹性伸缩AS、负载均衡ELB、镜像服务IMS、Cloud-Init）

## BGP 协议基础
- 路径矢量协议，AS之间使用，互联网骨干。
- TCP 179 建邻，增量更新+KeepAlive 60s，丰富路径属性选路。
- iBGP（同AS，需全互联或RR）、eBGP（不同AS，默认直连）。
- 五大选路：Weight → Local_Pref → 本地起源 → AS_Path短 → Origin。
```
bgp 65010
 router-id 1.1.1.1
 peer 10.0.0.2 as-number 65020
 network 192.168.1.0 24
```

## STP / RSTP 生成树
目的：消除二层环路，防广播风暴。
STP选举：根桥（Bridge ID最小）→ 根端口（到根开销最小）→ 指定端口 → 其余阻塞。
RSTP（802.1w）改进：收敛<1s；P/A机制；端口状态简化为Discarding/Learning/Forwarding；边缘端口跳过协商。
```
stp mode rstp
stp root primary
stp bpdu-protection
stp edged-port default
```

## ACL 访问控制列表
- 基本ACL（2000-2999）：仅匹配源IP
- 高级ACL（3000-3999）：匹配源/目IP、端口、协议
- 二层ACL（4000-4999）：MAC、VLAN
- 用户ACL（6000-6999）：用户/用户组
匹配顺序 config（默认）或 auto（按精确度）。方向 inbound/outbound。
```
acl 3000
 rule 5 permit tcp source 192.168.1.0 0.0.0.255 destination 10.0.0.0 0.0.0.255 destination-port eq 80
 rule 10 deny ip
interface GigabitEthernet0/0/1
 traffic-filter inbound acl 3000
```

## NAT 网络地址转换
- 静态NAT：1对1，服务器发布
- 动态NAT：地址池轮询
- NAPT/PAT：端口级，多主机共享1公网IP（家用最常见）
- Easy IP：地址池即出接口IP
- NAT Server（华为）：内网服务器端口映射公网
```
interface GigabitEthernet0/0/1
 ip address 1.1.1.1 24
 nat server protocol tcp global 1.1.1.1 80 inside 192.168.1.10 80
```

## 鸿蒙 ArkTS 入门
- 工具：DevEco Studio；语言：ArkTS（TypeScript超集）；UI框架：ArkUI（声明式）
```
@Entry
@Component
struct HelloPage {
  @State message: string = '华技云'
  build() {
    Column() {
      Text(this.message).fontSize(30)
      Button('点我').onClick(() => { this.message = 'Hello HarmonyOS' })
    }
  }
}
```
学习路径：环境搭建→组件化与状态管理（@State/@Prop/@Link）→路由与持久化→网络请求与Stage模型。

## 网络基础 TCP/IP
四层：应用层（HTTP/FTP/DNS/SMTP）、传输层（TCP可靠三次握手/UDP）、网络层（IP/ICMP/ARP/OSPF/BGP）、网络接口层。
TCP三次握手：SYN → SYN+ACK → ACK。四次挥手因全双工双向关闭；主动关闭方 TIME_WAIT 2MSL。
常用端口：21 FTP / 22 SSH / 23 Telnet / 25 SMTP / 53 DNS / 80 HTTP / 443 HTTPS / 3306 MySQL / 6379 Redis。

## IPv6 基础
- 128位地址，冒号十六进制，:: 表示连续0（仅一次）。
- 类型：单播、多播、任播（IPv6独有）。
- 过渡：双栈、隧道（6to4/GRE/ISATAP）、NAT64/DNS64。
- 自动配置 SLAAC + RA。优势：近乎无限地址、端到端可达无需NAT、即插即用、更好QoS/移动性。
