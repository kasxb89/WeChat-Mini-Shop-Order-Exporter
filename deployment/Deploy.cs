using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Deployment
{
    internal const string Version = "__VERSION__";
    internal const string PayloadHash = "__PAYLOAD_SHA256__";
    internal const string FileName = "微信小店订单导出器.exe";
    internal static string InstallDirectory
    {
        get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "WechatOrderExporter"); }
    }

    internal static Stream Payload()
    {
        Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("exporter-payload");
        if (stream == null) throw new InvalidDataException("安装包缺少程序文件。");
        return stream;
    }

    internal static string HashFile(string path)
    {
        using (Stream stream = File.OpenRead(path))
        using (SHA256 sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    internal static void VerifyPayload()
    {
        using (Stream stream = Payload())
        using (SHA256 sha = SHA256.Create())
        {
            string hash = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            if (hash != PayloadHash) throw new InvalidDataException("安装包校验失败，请重新获取完整安装包。");
        }
    }

    internal static string DeployFiles(string directory, Action<int> progress)
    {
        Directory.CreateDirectory(directory);
        string target = Path.Combine(directory, FileName);
        string temporary = Path.Combine(directory, ".exporter-" + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            using (Stream source = Payload())
            using (FileStream output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                byte[] buffer = new byte[1024 * 1024];
                long total = 0;
                int length;
                while ((length = source.Read(buffer, 0, buffer.Length)) > 0)
                {
                    output.Write(buffer, 0, length);
                    total += length;
                    progress((int)(80L * total / source.Length));
                }
                output.Flush(true);
            }
            if (HashFile(temporary) != PayloadHash) throw new InvalidDataException("程序写入后校验失败，请重试。");
            progress(90);
            if (File.Exists(target)) File.Replace(temporary, target, target + ".previous");
            else File.Move(temporary, target);
            using (Stream usage = Assembly.GetExecutingAssembly().GetManifestResourceStream("exporter-usage"))
            using (FileStream output = File.Create(Path.Combine(directory, "使用说明.txt")))
                usage.CopyTo(output);
            return target;
        }
        finally
        {
            // 只清理本次创建的临时文件，不触碰订单目录、微信或其他用户文件。
            if (File.Exists(temporary)) File.Delete(temporary);
        }
    }

    internal static void EnsureNotRunning()
    {
        foreach (Process process in Process.GetProcessesByName(Path.GetFileNameWithoutExtension(FileName)))
        {
            using (process)
                if (!process.HasExited)
                    throw new InvalidOperationException("请先保存并退出正在运行的微信小店订单导出器，再点击部署。");
        }
    }

    internal static void CreateShortcut(string location, string executable)
    {
        object shellObject = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        object shortcutObject = null;
        try
        {
            dynamic shell = shellObject;
            shortcutObject = shell.CreateShortcut(location);
            dynamic shortcut = shortcutObject;
            shortcut.TargetPath = executable;
            shortcut.WorkingDirectory = Path.GetDirectoryName(executable);
            shortcut.IconLocation = executable + ",0";
            shortcut.Description = "微信小店订单导出器";
            shortcut.Save();
        }
        finally
        {
            if (shortcutObject != null) Marshal.FinalReleaseComObject(shortcutObject);
            Marshal.FinalReleaseComObject(shellObject);
        }
    }

    internal static void CreateShortcuts(string executable)
    {
        string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        string name = File.Exists(Path.Combine(desktop, FileName)) ? "微信小店订单导出器（已部署）.lnk" : "微信小店订单导出器.lnk";
        CreateShortcut(Path.Combine(desktop, name), executable);
        string programs = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "微信小店订单导出器");
        Directory.CreateDirectory(programs);
        CreateShortcut(Path.Combine(programs, "微信小店订单导出器.lnk"), executable);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 1 && args[0] == "--verify") { VerifyPayload(); return 0; }
#if QA
            if (args.Length == 2 && args[0] == "--self-test")
            {
                string folder = Path.GetFullPath(args[1]);
                Directory.CreateDirectory(folder);
                string marker = Path.Combine(folder, "保留数据.txt");
                File.WriteAllText(marker, "preserved", Encoding.UTF8);
                string target = DeployFiles(folder, delegate(int value) {});
                DeployFiles(folder, delegate(int value) {});
                if (HashFile(target) != PayloadHash || HashFile(target + ".previous") != PayloadHash)
                    throw new InvalidDataException("部署文件不一致");
                if (File.ReadAllText(marker, Encoding.UTF8) != "preserved") throw new InvalidDataException("部署改变了保留数据");
                string linkPath = Path.Combine(folder,"部署验证.lnk");
                CreateShortcut(linkPath,target);
                object shellObject = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
                object linkObject = null;
                try
                {
                    dynamic shell = shellObject;
                    linkObject = shell.CreateShortcut(linkPath);
                    dynamic link = linkObject;
                    if (Path.GetFullPath((string)link.TargetPath) != target) throw new InvalidDataException("快捷方式目标不一致");
                }
                finally
                {
                    if (linkObject != null) Marshal.FinalReleaseComObject(linkObject);
                    Marshal.FinalReleaseComObject(shellObject);
                }
                return 0;
            }
#endif
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (!Environment.Is64BitOperatingSystem || Environment.OSVersion.Version.Major < 10)
                throw new NotSupportedException("此版本用于 Windows 10/11 64 位系统。");
            Application.Run(new SetupForm());
            return 0;
        }
        catch (Exception error)
        {
            if (args.Length == 0) MessageBox.Show(error.Message, "部署未完成", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }
}

internal sealed class SetupForm : Form
{
    private readonly Button deploy = new Button();
    private readonly ProgressBar progress = new ProgressBar();
    private readonly Label status = new Label();
    private readonly CheckBox launch = new CheckBox();
    private bool busy;
    private bool completed;

    internal SetupForm()
    {
        Text = "微信小店订单导出器 · 一键部署 " + Deployment.Version;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(680, 410);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        AutoScaleMode = AutoScaleMode.Dpi;
        Font = new Font("Microsoft YaHei", 10);
        BackColor = Color.FromArgb(28, 36, 32);
        ForeColor = Color.FromArgb(237, 244, 240);
        Label title = AddLabel("一键部署，即可开始。", 24, 24, 632, 38);
        title.Font = new Font("Microsoft YaHei", 20, FontStyle.Bold);
        AddLabel("已内置运行库与本地识别模型，无需联网安装开发环境。\n微信客户端请自行安装并登录。", 24, 82, 632, 55);
        AddLabel("部署位置", 24, 149, 632, 24);
        Label folder = AddLabel(Deployment.InstallDirectory, 24, 177, 632, 43);
        folder.ForeColor = Color.FromArgb(181, 196, 187);
        AddLabel("首次运行起试用 7 天，激活一次后长期有效。\n未激活到期只停用采集，订单和 Excel 保留。", 24, 225, 632, 49);
        progress.SetBounds(24, 283, 632, 14);
        Controls.Add(progress);
        status.SetBounds(24, 306, 632, 25);
        status.Text = "准备就绪 · 部署到当前用户，无需管理员权限";
        Controls.Add(status);
        launch.SetBounds(24, 352, 320, 28);
        launch.Text = "部署完成后打开程序";
        launch.Checked = true;
        Controls.Add(launch);
        deploy.SetBounds(448, 341, 208, 44);
        deploy.Text = "部署并创建快捷方式";
        deploy.FlatStyle = FlatStyle.Flat;
        deploy.BackColor = Color.FromArgb(133, 216, 182);
        deploy.ForeColor = Color.FromArgb(16, 39, 29);
        deploy.Click += OnDeploy;
        Controls.Add(deploy);
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) e.Cancel = true; };
    }

    private Label AddLabel(string value, int x, int y, int width, int height)
    {
        Label label = new Label();
        label.Text = value;
        label.SetBounds(x, y, width, height);
        Controls.Add(label);
        return label;
    }

    private void Report(int value)
    {
        BeginInvoke((Action)delegate { progress.Value = Math.Min(100, value); });
    }

    private async void OnDeploy(object sender, EventArgs e)
    {
        if (completed) { Close(); return; }
        if (busy) return;
        busy = true;
        deploy.Enabled = false;
        launch.Enabled = false;
        status.Text = "正在核对并部署程序…";
        try
        {
            string executable = await Task.Run(delegate
            {
                Deployment.EnsureNotRunning();
                string target = Deployment.DeployFiles(Deployment.InstallDirectory, Report);
                return target;
            });
            Deployment.CreateShortcuts(executable);
            progress.Value = 100;
            status.Text = "部署完成，桌面和开始菜单快捷方式已创建。";
            completed = true;
            deploy.Text = "关闭";
            if (launch.Checked)
                Process.Start(new ProcessStartInfo(executable) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(executable) });
        }
        catch (Exception error)
        {
            status.Text = "部署未完成，可处理后重试。";
            MessageBox.Show(this, error.Message, "部署未完成", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        finally
        {
            busy = false;
            deploy.Enabled = true;
            launch.Enabled = true;
        }
    }
}
