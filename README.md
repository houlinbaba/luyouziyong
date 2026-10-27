# 路由器面板 v2 (NeoServer 式卡片 + 终端)

部署在日本 VPS 本机上,不是 Docker 里跑,这样本地卡片和本地终端
就是 VPS 本身。路由器走 SSH 轮询,路由器上不用装任何东西。

v2 改动（按 NeoServer 7.9.1 实测分析改的）：
- 删掉 paramiko，改走系统 ssh + sshpass，带 HostKeyAlgorithms=+ssh-rsa，
  能连小米 dropbear 那个只有 ssh-rsa 的主机密钥——v1 就是死在这一步。
- 温度按 NeoServer 的 fallback 链：hwmon → thermal → /proc/dmu/temperature（小米）→ hwmon。
- 卡片多显示 CPU 型号、网关接口、系统版本。

## 文件
- app.py: 服务端,卡片数据 + 终端 websocket
- index.html: 卡片界面,3 秒自动刷新
- hosts.example.json: 主机示例,复制成 hosts.json 后填密码
- requirements.txt: flask, flask-sock（不再需要 paramiko；另需系统包 sshpass）

## VPS 上部署
1. git clone 到 /opt/router-panel,不要手改 hosts.json!
   hosts.json 由面板自己写,手改 JSON 很容易写坏导致面板起不来。
2. 唯一要手改的文件是 router-panel.service 里的 PANEL_PASSWORD
   (面板登录密码),改成你自己的,其他文件都不用动。
3. 装 sshpass + pip 依赖,systemd 启动,见助手给你的纯命令。
4. 打开 http://129.225.134.59:8080,先在登录页输面板密码,
   打不开先查甲骨文云安全组和 VPS 防火墙有没有放行 8080。
5. 路由器直接在面板里加,点“＋ 添加主机”,不要改文件:
   - 主路由:主机名 主路由.1,IP 127.0.0.1,端口 51101,账号 root,密码填主路由 SSH 密码
   - 副节点:主机名 副节点.182,IP 127.0.0.1,端口 51149,账号 root,密码填.182 SSH 密码
   - AX6S:主机名 AX6S,IP 127.0.0.1,端口 51228,账号 root,密码填 AX6S SSH 密码
   (frp 拓扑 2026-10-27:51101->主路由 .1:22,51149->副节点 .182:22,
   51228->AX6S:22230;从 VPS 本机看就是 127.0.0.1 + 上面三个端口)
   面板保存后自动写回 hosts.json,卡片 3 秒后就开始出数。

## 后台常驻（关终端也不掉）
前台跑 `python3 app.py` 时，关掉终端面板就没了，那是正常的，
不是面板坏了。要长期挂着，用 systemd：
1. 把 router-panel.service 里的 PANEL_PASSWORD 改成你自己的密码
2. 按助手给你的 systemd 命令装服务、开机自启
装好后关终端、重启 VPS，面板都会自动起来，地址不变。

## 界面
- 顶部横幅一次只显示一台主机，◀ ▶ 切换，不会把所有卡堆在同一页
- 卡片下方有“删除此主机”，甲骨文本机不能删，路由器可以删，
  删完直接写回 hosts.json，不用重启

## 注意
- 面板密码 PANEL_PASSWORD 一定要改,不要用默认或空密码暴露公网
- hosts.json 里有路由器 SSH 密码,只放 VPS 上,不要发给别人
- CPU / 网络 / I/O 速率是两次轮询的差值,第一次打开显示 -,
  3 秒后刷新即正常
- 路由器卡片离线时,先在手机 NeoServer 里确认同一 frp 端口能连,
  再看 frp 隧道是否在线
