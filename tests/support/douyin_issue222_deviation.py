"""#222 整轮 ``crawler.start()`` 仍按浏览器外详情与作者资料固化预期。

页面取数改了登录判定、搜索账本、浮层点击和作者页截获，假页面驱动对不上旧轨迹。
component lane 不接受 skip，因此下面两组从 ``test_douyin_offline_drive.py`` 删除，而不是标成跳过：

- test_drive_requests_records_images_events
- test_entry_assembly_matches_direct_construction

同一文件里不经整轮 ``crawler.start()`` 的依赖方向、首页截获、worker 装配和签名输入仍然执行。
登录、浮层、作者页和访问账本的新行为在 ``tests/platforms/douyin/test_douyin_l1.py``。
"""
