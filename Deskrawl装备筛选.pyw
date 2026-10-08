from pathlib import Path
import traceback

try:
    from deskrawl_assistant.app import main
    main()
except Exception:
    error = traceback.format_exc()
    target = Path(__file__).resolve().parent / 'data/runtime/app-error.log'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(error, encoding='utf-8')
    from tkinter import messagebox
    messagebox.showerror('装备助手无法启动', f'启动错误已记录到：\n{target}')
