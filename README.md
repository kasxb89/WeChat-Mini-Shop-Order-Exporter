# WeChat-Mini-Shop-Order-Exporter

微信小店个人订单导出器，独立 Windows 桌面程序。按下单时间和店铺筛选购买记录，读取商品、订单编号、快递单号、收件人、联系电话和收货地址，导出 Excel。

## 采集流程

1. 选择已登录微信的账号窗口，设置时间范围、店铺关键词和保存目录。
2. 程序通过微信原生 `Ctrl+F` 聚焦搜索，自动打开或重开小店订单中心，建立当前账号的本机数据连接。聚焦搜索不依赖主窗口的截图、字号或缩放。
3. 分页读取订单列表，筛选后最多同时读取 4 笔详情，从订单数据取得物流和收货信息。
4. 保存本地记录及 Excel。准备完成后可使用其他软件，订单采集阶段不逐单点击或滚动页面。

支持多开微信选择、历史任务、暂停续采、订单去重、多包裹单号、记录清除前备份，以及导出后打开文件夹。

「重新检测」会显示检测结果，只有一个窗口时自动选择。支持主窗口标题带账号后缀、托盘隐藏的主窗口和不同安装目录；读取小店数据仍需兼容的小程序运行时。检测不到时可打开「检测详情」，复制进程、窗口和错误信息给技术支持；同一报告保存到本机 `last-wechat-detection.json`，不自动上传。

自动准备优先复用同一账号的小店搜索页；需要搜索时依次使用快捷键、快捷键重试、控件或局部图像定位。短暂失焦会自动激活所选微信，页面加载期间继续等待。旧小店会话未就绪时会重建一次连接，从同一账号的主窗口重新准备。F8 停止、窗口关闭和需要登录时仍会停止操作。

待发货等没有物流的订单也会导出收件人、联系电话和收货地址。脱敏姓名、手机号按平台返回保存，不猜测补全；未返回的字段留空。旧任务点击「继续上次」可补采收货信息，更新原 Excel，不重复增加订单。

## 当前适配范围

- Windows 10/11 64 位，开发及打包使用 Python 3.12。
- 内置 Windows x64 小程序运行时 **25710、25715、26073** 三套布局，按实际运行库版本自动匹配，不限制微信主程序必须是某个小版本。26073 已通过本机真实订单 API 验证；25710、25715 的布局来自下列上游配置，完整收发链路仍需对应客户端验证。
- 支持从启动参数或已加载模块定位运行库，兼容启动器与实际运行库不在同一目录的安装方式。检测详情包含微信进程文件版本。
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

测试使用虚构样例，不会操作真实微信窗口或向联系人发送消息。覆盖时间筛选、分页、去重续采、后台准备、编号精度、Excel、历史记录和激活流程。订单 JavaScript 字段投影测试需要 Node.js；未安装时跳过该组测试。

## 打包

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --distpath dist/0.9.3 --workpath build/0.9.3 exporter.spec
.\.venv\Scripts\python.exe deployment/build_deployer.py
```

输出：

- `dist/0.9.3/微信小店订单导出器.exe`：直接运行的程序。
- `dist/0.9.3/微信小店订单导出器-一键部署.exe`：当前用户安装程序，创建快捷方式并保留已有采集记录。

一键部署包包含运行组件，客户无需安装 Python 或 Office，微信需自行安装并登录。部署器构建需要 Windows 自带的 .NET Framework C# 编译器；构建后会执行包校验及隔离目录中的安装、升级检查。

## 目录与数据

- `order_exporter/`：界面、订单数据连接、采集、存储与 Excel。
- `deployment/`：本机构建配置及一键部署器。
- `tests/`：可重复运行的合成数据测试。
- `DESIGN.md`、`使用说明.txt`：设计约定与使用说明。

运行记录位于 `%LOCALAPPDATA%/WechatOrderExporter/`。订单、Excel、截图、调试录制、第三方小程序缓存及本机私有配置均不提交。程序不上传订单，不调用大模型。

第三方依赖许可见 `order_exporter/assets/licenses/`。

25710、25715 布局数据来源：[WMPFDebugger](https://github.com/evi0s/WMPFDebugger/tree/9be0b2d0bd9875bd21a104a2279ad4304b46388b/frida/config/win32)，保留原配置及 GPLv2 许可说明。未将上游自动分析脚本并入客户端。
