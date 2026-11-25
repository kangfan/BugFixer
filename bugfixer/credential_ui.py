"""User-operated local credential entry; never emits passwords to stdout."""
def run(database):
    import tkinter as tk
    from tkinter import ttk, messagebox
    from .credentials import save
    from .common import BugFixerError

    window = tk.Tk()
    window.title('BugFixer — 本机网页登录凭据')
    window.resizable(False, False)
    fields = {}
    defaults = {'name': 'zentao', 'source': 'zentao', 'environment': 'production', 'login_url': 'http://zen.wckj.com/user-login.html', 'account': '', 'password': ''}
    labels = {'name': '凭据名称', 'source': '系统', 'environment': '环境', 'login_url': '登录URL', 'account': '账号', 'password': '密码'}
    for i, (key, value) in enumerate(defaults.items()):
        ttk.Label(window, text=labels[key]).grid(row=i, column=0, padx=10, pady=6, sticky='w')
        var = tk.StringVar(value=value)
        fields[key] = var
        if key in ('source', 'environment'):
            choices = ('zentao', 'customer_service') if key == 'source' else ('production', 'test')
            widget = ttk.Combobox(window, textvariable=var, values=choices, state='readonly', width=51)
        else:
            widget = ttk.Entry(window, textvariable=var, width=54, show='*' if key == 'password' else '')
        widget.grid(row=i, column=1, padx=10, pady=6)
    def suggest(*_):
        source, env = fields['source'].get(), fields['environment'].get()
        fields['name'].set(('zentao' if source=='zentao' else 'customer_service') + ('_test' if env=='test' else ''))
        fields['login_url'].set('http://zen.wckj.com/user-login.html' if source=='zentao' else 'https://cscmanage.galasports.com/html/index.html' if env=='production' else 'http://cscmanage.test-galasports.com/html/index.html')
    fields['source'].trace_add('write',suggest)
    fields['environment'].trace_add('write',suggest)
    def store():
        try:
            values = {k: v.get() for k, v in fields.items()}
            save(database, values['name'], values['source'], values['environment'], values['login_url'], values['account'], values['password'])
            fields['password'].set('')
            messagebox.showinfo('BugFixer', '已加密保存到 SQLite。可继续填写另一系统；完成后关闭窗口。')
        except BugFixerError as e:
            messagebox.showerror('BugFixer', e.message)
    ttk.Label(window, text='密码使用当前Windows用户DPAPI加密；不会显示或写入日志。').grid(row=6, column=0, columnspan=2, padx=10, pady=6)
    ttk.Button(window, text='加密保存', command=store).grid(row=7, column=1, padx=10, pady=10, sticky='e')
    window.mainloop()
