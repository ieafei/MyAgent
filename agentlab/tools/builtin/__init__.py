"""内置工具。

目前只有文件系统一组，且**不在这里 re-export**：

    from agentlab.tools.builtin.fs import make_file_tools

工具需要工作区根目录才能构造。如果 import 一个包就拿到一屋子已经生效的
写文件权限，"谁在什么时候获得了写权限"就说不清了；显式构造才看得见。
"""
