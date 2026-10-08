"""Offline installation window; only explicit Apply writes an installation."""
import argparse
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from agent_bridge import install_manager as installs


class Installer:
    def __init__(self, root, payload, initial_root=None):
        self.root, self.payload = root, payload
        self.plan = None
        self.busy = False
        self.events = queue.Queue()
        self.controls = []
        self.versions = {}
        root.title('PatchPort Setup')
        root.geometry('860x670')
        root.minsize(760, 580)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='PatchPort Setup', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Install a new version alongside existing versions. Review before applying.',
                  wraplength=790).pack(anchor='w', pady=(4, 14))
        row = ttk.Frame(frame)
        row.pack(fill='x')
        ttk.Label(row, text='Install folder:').pack(side='left', padx=(0, 8))
        default = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'PatchPort/App'
        self.destination = tk.StringVar(value=str(initial_root or default))
        entry = ttk.Entry(row, textvariable=self.destination)
        entry.pack(side='left', fill='x', expand=True)
        self.controls.append(entry)
        self.button(row, 'Choose\u2026', self.browse)
        self.destination.trace_add('write', self.invalidate)
        self.shortcut = tk.BooleanVar(value=True)
        check = ttk.Checkbutton(frame, text='Create a version-specific shortcut inside the install folder',
                                variable=self.shortcut, command=self.invalidate)
        check.pack(anchor='w', pady=8)
        self.controls.append(check)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=5)
        self.button(row, 'Preview install / upgrade', self.preview)
        self.button(row, 'List installed versions', self.list_versions)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=5)
        self.version = tk.StringVar()
        self.selector = ttk.Combobox(row, textvariable=self.version, state='readonly')
        self.selector.pack(side='left', fill='x', expand=True)
        self.selector.bind('<<ComboboxSelected>>', self.invalidate)
        self.button(row, 'Preview GUI removal', self.remove)
        ttk.Label(frame, text=installs.KEEP_NOTE, wraplength=790).pack(anchor='w', pady=8)
        output = ttk.Frame(frame)
        output.pack(fill='both', expand=True)
        self.text = tk.Text(output, height=10, state='disabled', wrap='word', font=('Consolas', 10))
        scroll = ttk.Scrollbar(output, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(fill='both', expand=True)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=10)
        self.apply_button = self.button(row, 'Apply preview\u2026', self.apply)
        self.button(row, 'Discard preview', self.invalidate)
        self.status = tk.StringVar(value='No changes made. Installing does not start a runner or invoke AI.')
        ttk.Label(frame, textvariable=self.status, wraplength=790).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        self.update_controls()

    def button(self, parent, label, callback):
        widget = ttk.Button(parent, text=label, command=lambda: self.guard(callback))
        widget.pack(side='left', padx=(0, 6))
        self.controls.append(widget)
        return widget

    def guard(self, callback):
        try:
            callback()
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror('PatchPort Setup', str(exc), parent=self.root)

    def invalidate(self, *args):
        self.plan = None
        if hasattr(self, 'status'):
            self.status.set('Preview discarded. Review a new preview before applying.')
            self.update_controls()

    def browse(self):
        folder = filedialog.askdirectory(parent=self.root, title='Choose a dedicated installation folder')
        if folder:
            self.destination.set(folder)

    def run(self, kind, operation):
        if self.busy:
            raise RuntimeError('An installation operation is pending')
        self.busy = True
        self.status.set('Working. Please keep this window open until the result is shown.')
        self.update_controls()
        def worker():
            try:
                self.events.put((kind, operation(), None))
            except Exception as exc:
                self.events.put((kind, None, str(exc)))
        threading.Thread(target=worker, daemon=False).start()

    def preview(self):
        self.plan = None
        folder, shortcut = Path(self.destination.get()), self.shortcut.get()
        self.run('preview', lambda: installs.preview_install(self.payload, folder, shortcut=shortcut))

    def list_versions(self):
        self.plan = None
        folder = Path(self.destination.get())
        self.run('list', lambda: installs.installed_versions(folder))

    def remove(self):
        if not self.version.get():
            raise ValueError('List installed versions and select one first')
        folder, key = Path(self.destination.get()), self.versions[self.version.get()]
        self.plan = None
        self.run('preview', lambda: installs.preview_remove(folder, key))

    def apply(self):
        if self.plan is None:
            raise ValueError('Preview first')
        if messagebox.askyesno('Apply displayed changes',
                'Apply exactly the displayed changes?\n\n' + installs.KEEP_NOTE, parent=self.root):
            plan, self.plan = self.plan, None
            self.run('apply', lambda: installs.apply(plan))

    def show(self, value):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        if isinstance(value, list):
            text = '\n'.join(item['version'] + ': ' + item['state'] + '\n' + item['directory'] for item in value)
        else:
            text = '\n'.join(key.replace('_', ' ').capitalize() + ': ' +
                             ('\n  ' + '\n  '.join(str(p) for p in item) if isinstance(item, list) else str(item))
                             for key, item in value.items())
        self.text.insert('1.0', text or 'No installed versions found.')
        self.text.configure(state='disabled')

    def poll(self):
        try:
            kind, value, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if error:
                self.plan = None
                self.status.set('No automatic retry. Inspect the error and preview again.')
                self.show(dict(error=error))
            elif kind == 'preview':
                self.plan = value
                self.show(value.report)
                self.status.set('Preview only. Review the file changes and retention policy, then Apply.')
            elif kind == 'list':
                self.versions = {v['key'] + ' \u2014 ' + v['state']: v['key'] for v in value}
                self.selector.configure(values=list(self.versions))
                self.version.set('')
                self.show(value)
                self.status.set('Select a version to preview GUI removal. Core and projects are retained.')
            else:
                self.show(value)
                self.status.set('Outcome: ' + value['outcome'] + '. Open the installed GUI to reconnect selected projects.')
            self.update_controls()
        self.root.after(100, self.poll)

    def update_controls(self):
        for widget in self.controls:
            widget.configure(state='disabled' if self.busy else 'normal')
        self.selector.configure(state='disabled' if self.busy else 'readonly')
        self.apply_button.configure(state='normal' if self.plan and not self.busy else 'disabled')

    def close(self):
        if self.busy:
            self.status.set('An operation is pending. Wait for its result before closing; changes are not rolled back.')
            return
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='PatchPort offline desktop installer')
    parser.add_argument('--payload', type=Path, help='Explicit package for source development only')
    parser.add_argument('--install-root', type=Path, help='Initial installation folder; preview still required')
    args = parser.parse_args()
    if getattr(sys, 'frozen', False):
        if args.payload:
            parser.error('Packaged installers use their embedded payload')
        payload = Path(sys._MEIPASS) / 'payload/payload.zip'
    else:
        if args.payload is None:
            parser.error('Source execution requires --payload')
        payload = args.payload.resolve()
    root = tk.Tk()
    Installer(root, payload, args.install_root)
    root.mainloop()


if __name__ == '__main__':
    main()
