from __future__ import annotations

import os
import sys

from order_exporter.native import enable_dpi, close_bridge


def main():
    enable_dpi()
    from PySide6 import QtGui, QtWidgets, QtCore
    from order_exporter.ui import MainWindow, configure_app
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("微信小店订单导出器")
    app.setOrganizationName("客服工具")
    # 同一工具只运行一个实例，避免两个采集线程同时操作微信。
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    socket = QLocalSocket()
    socket.connectToServer("WechatOrderExporter-1")
    if socket.waitForConnected(200):
        socket.write(b"show")
        socket.waitForBytesWritten(200)
        return 0
    QLocalServer.removeServer("WechatOrderExporter-1")
    server = QLocalServer()
    if not server.listen("WechatOrderExporter-1"):
        return 1
    configure_app(app)
    app.aboutToQuit.connect(close_bridge)
    window = MainWindow()

    def incoming():
        connection = server.nextPendingConnection()
        if connection:
            connection.disconnectFromServer()
            window.showNormal()
            window.raise_()
            window.activateWindow()
            connection.deleteLater()
    server.newConnection.connect(incoming)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
