\# Faceit Pro Tracker (Render 版)



\## 部署步骤（Render）

1\. 把本仓库连接到 Render，选择 \*\*Web Service\*\*。

2\. 环境变量里新增：

&nbsp;  - `FACEIT\_API\_KEY` = 你的 Faceit Server-side API Key

3\. 其它保持默认，点击 Deploy。

4\. 打开服务地址，即可看到前端页面，点击“刷新数据”加载示例玩家。



后端接口：

\- `GET /players?query=<nickname>`

\- `GET /matches/with\_stats?player\_id=<id>\&limit=10`



