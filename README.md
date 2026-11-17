# WeChat-Mini-Shop-Order-Exporter

微信小店个人订单导出器，独立 Windows 桌面程序。按下单时间和店铺筛选购买记录，读取商品、订单编号、快递单号和收件人，导出 Excel。

## 采集流程

1. 选择已登录微信的账号窗口，设置时间范围、店铺关键词和保存目录。
2. 程序自动搜索并打开或重开小店订单中心，建立当前账号的本机数据连接。
3. 分页读取订单列表，筛选后最多同时读取 4 笔详情，从订单数据取得物流和收件人。
4. 保存本地记录及 Excel。准备完成后可使用其他软件，订单采集阶段不逐单点击或滚动页面。

支持多开微信选择、历史任务、暂停续采、订单去重、多包裹单号、记录清除前备份，以及导出后打开文件夹。

## 当前适配范围

- Windows 10/11 64 位，开发及打包使用 Python 3.12。
- 微信小程序运行时 **RadiumWMPF 26073**，本机适配检查包含运行库摘要。
- 数据读取使用已登录微信的小程序业务上下文，当前调用 `getorderlist` 和 `getorderdetail`。它不是面向商家的公开订单 API。
- 微信运行库或小店内部模块变化后需要重新适配；不支持的版本会暂停并保留进度。
- 处理个人账号购买记录，不代表商家后台全体买家的订单。只读取订单，不执行支付、确认收货、退款或删除订单。

## 从源码运行

在 Windows PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
```

源码默认可以试用。正式构建前由部署方配置激活码：

```powershell
.\.venv\Scripts\python.exe deployment/configure_activation.py
```

该命令交互输入激活码，只在本机生成 `order_exporter/_private_config.py`，此文件已被 Git 忽略。仓库不包含正式激活码或本机授权记录。无人值守构建可通过 `ORDER_EXPORTER_ACTIVATION_CODE` 环境变量提供值，再执行 `configure_activation.py --from-env`。

客户端离线激活用于本工具的试用和开通流程，不应当替代服务端的商业授权校验。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用虚构样例，不会操作真实微信窗口或向联系人发送消息。覆盖时间筛选、分页、去重续采、后台准备、编号精度、Excel、历史记录和激活流程。

## 打包

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist/0.9.0 --workpath build/0.9.0 exporter.spec
.\.venv\Scripts\python.exe deployment/build_deployer.py
```

输出：

- `dist/0.9.0/微信小店订单导出器.exe`：直接运行的程序。
- `dist/0.9.0/微信小店订单导出器-一键部署.exe`：当前用户安装程序，创建快捷方式并保留已有采集记录。

一键部署包包含运行组件，客户无需安装 Python 或 Office，微信需自行安装并登录。部署器构建需要 Windows 自带的 .NET Framework C# 编译器；构建后会执行包校验及隔离目录中的安装、升级检查。

## 目录与数据

- `order_exporter/`：界面、订单数据连接、采集、存储与 Excel。
- `deployment/`：本机构建配置及一键部署器。
- `tests/`：可重复运行的合成数据测试。
- `DESIGN.md`、`使用说明.txt`：设计约定与使用说明。

运行记录位于 `%LOCALAPPDATA%/WechatOrderExporter/`。订单、Excel、截图、调试录制、第三方小程序缓存及本机私有配置均不提交。程序不上传订单，不调用大模型。

第三方依赖许可见 `order_exporter/assets/licenses/`。
