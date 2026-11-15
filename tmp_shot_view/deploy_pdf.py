# -*- coding: utf-8 -*-
import shutil, time
src = r"e:\supplyguard\tmp_shot_view\v11.pdf"
dst = r"e:\supplyguard\技术文档\SupplyGuard_技术文档_v1.1.pdf"
for i in range(150):
    try:
        shutil.copy2(src, dst)
        print("COPIED OK after %ds" % (i * 2))
        break
    except OSError:
        time.sleep(2)
else:
    print("TIMEOUT: file still locked")
