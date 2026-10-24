#import "PyServer.h"
#import <dlfcn.h>
#import <Foundation/Foundation.h>

@implementation PyServer

+ (void)startServerWithBundlePath:(NSString *)bundlePath
                     writablePath:(NSString *)writablePath {
    @autoreleasepool {
        // gh238: pyhome/server 改成从 Frameworks/ 下读（见 CMakeLists gh238 注释）。
        // iOS 16 的 Library Validation 要求 bundle 内每个被 dlopen 的 .so 都与
        // 主程序同证书签名；重签工具只遍历 Frameworks/，故运行时也放那里。
        NSString *fwPath = [bundlePath stringByAppendingPathComponent:@"Frameworks/Python.framework"];
        NSString *dylib = [fwPath stringByAppendingPathComponent:@"Python"];
        NSString *pyHome = [bundlePath stringByAppendingPathComponent:@"Frameworks/pyhome"];
        NSString *serverSrc = [bundlePath stringByAppendingPathComponent:@"Frameworks/server/app"];
        // 服务端跑在 Documents（bundle 只读；状态/存档/日志都落在可写目录）
        NSString *serverDst = [writablePath stringByAppendingPathComponent:@"jhserver"];

        // ---- 前置自检：缺件直接点名（进 launch.log 诊断）----
        NSFileManager *fm = [NSFileManager defaultManager];
        NSArray *need = @[fwPath, dylib, pyHome,
                          [pyHome stringByAppendingPathComponent:@"lib/python3.14/os.py"],
                          [pyHome stringByAppendingPathComponent:@"lib/python3.14/lib-dynload"],
                          serverSrc];
        for (NSString *p in need) {
            if (![fm fileExistsAtPath:p]) {
                fprintf(stderr, "[PyServer] MISSING: %s\n", [p UTF8String]);
            }
        }

        NSLog(@"[PyServer] dlopen: %@", dylib);
        void *handle = dlopen([dylib fileSystemRepresentation], RTLD_NOW | RTLD_GLOBAL);
        if (!handle) {
            fprintf(stderr, "[PyServer] dlopen failed: %s\n", dlerror());
            return;
        }
        NSLog(@"[PyServer] Python.framework loaded");

        void (*py_initialize)(void) = (void(*)(void))dlsym(handle, "Py_Initialize");
        int (*py_run)(const char *) = (int(*)(const char *))dlsym(handle, "PyRun_SimpleString");
        const char * (*py_ver)(void) = (const char *(*)())dlsym(handle, "Py_GetVersion");
        if (!py_initialize || !py_run) {
            fprintf(stderr, "[PyServer] dlsym failed for core functions\n");
            return;
        }

        // ---- 环境变量（Py_Initialize 之前设置）----
        // pyhome 标准布局：$PYTHONHOME/lib/python3.14/{*.py,lib-dynload/}
        setenv("PYTHONHOME", [pyHome fileSystemRepresentation], 1);
        setenv("PYTHONPATH", [serverDst fileSystemRepresentation], 1);
        setenv("PYTHONUNBUFFERED", "1", 1);
        // 服务端路径覆盖（assemble_ios_server.py 注入的开关）：
        //   表读 bundle res 树（与 client_fz 内容全量一致），ink 扫描同理
        setenv("WJJH_RES_ROOT", [bundlePath fileSystemRepresentation], 1);
        setenv("WJJH_BUNDLE_ROOT", [bundlePath fileSystemRepresentation], 1);
        setenv("WJJH_DATA_DIR", [writablePath UTF8String], 1);

        NSLog(@"[PyServer] PYTHONHOME=%s", [pyHome fileSystemRepresentation]);
        if (py_ver) NSLog(@"[PyServer] Python version: %s", py_ver());

        py_initialize();
        NSLog(@"[PyServer] Python initialized");

        // 拷贝与启动都在 Python 侧做（bundle 只读 → 每次启动重铺到 Documents）。
        // ★ 诊断优先：sys.stderr 先重定向到文件（嵌入环境 stderr 链路不可信），
        //   全程 try/except 把 traceback 落盘，CI 用 simctl get_app_container 取回
        NSString *runner = [NSString stringWithFormat:
            @"import os, shutil, sys, traceback, runpy\n"
            "src = r'%@'\n"
            "dst = r'%@'\n"
            "logf = open(os.path.join(os.path.dirname(dst), 'py_err.log'), 'w', buffering=1)\n"
            "sys.stdout = logf\n"
            "sys.stderr = logf\n"
            "try:\n"
            "    sys.argv = ['fz_boot_server.py', '--port', '7900']\n"
            "    target = 'fz_boot_server.py' if os.path.exists(os.path.join(src, 'fz_boot_server.py')) else 'stub_server.py'\n"
            "    print('[PyServer] copying server ->', dst, flush=True)\n"
            "    shutil.rmtree(dst, ignore_errors=True)\n"
            "    shutil.copytree(src, dst)\n"
            "    print('[PyServer] server mode:', target, flush=True)\n"
            "    sys.path.insert(0, dst)\n"
            "    import importlib; importlib.invalidate_caches()\n"
            "    print('[PyServer] sys.path:', sys.path, flush=True)\n"
            "    print('[PyServer] dst listing:', sorted(os.listdir(dst)), flush=True)\n"
            "    print('[PyServer] cryptography dir:', sorted(os.listdir(os.path.join(dst, 'cryptography'))) if os.path.isdir(os.path.join(dst, 'cryptography')) else 'MISSING', flush=True)\n"
            "    import cryptography\n"
            "    print('[PyServer] cryptography loaded from:', cryptography.__file__, flush=True)\n"
            "    # gh319: 单机安卓v46 客户端把授权闸硬编码到 127.0.0.1:7901（见 MainScene.lua），\n"
            "    #   而本服务端监听 7900 ⇒ 授权请求打不通 ⇒ 空响应 ⇒ json.decode 抛错 ⇒ 黑屏。\n"
            "    #   这里起一个 7901->7900 的 TCP 转发线程，两端口都通（服务端逻辑零改动）。\n"
            "    def _wjjh_port_bridge():\n"
            "        import socket, threading\n"
            "        def _pipe(a, b):\n"
            "            try:\n"
            "                while True:\n"
            "                    d = a.recv(65536)\n"
            "                    if not d: break\n"
            "                    b.sendall(d)\n"
            "            except Exception: pass\n"
            "            finally:\n"
            "                try: a.close()\n"
            "                except Exception: pass\n"
            "                try: b.close()\n"
            "                except Exception: pass\n"
            "        def _accept_loop():\n"
            "            try:\n"
            "                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "                srv.bind(('127.0.0.1', 7901)); srv.listen(32)\n"
            "                print('[PyServer] gh319: 7901->7900 bridge listening', flush=True)\n"
            "                while True:\n"
            "                    c, _ = srv.accept()\n"
            "                    try:\n"
            "                        u = socket.create_connection(('127.0.0.1', 7900), timeout=10)\n"
            "                    except Exception as e:\n"
            "                        print('[PyServer] gh319: upstream fail', repr(e), flush=True)\n"
            "                        try: c.close()\n"
            "                        except Exception: pass\n"
            "                        continue\n"
            "                    threading.Thread(target=_pipe, args=(c, u), daemon=True).start()\n"
            "                    threading.Thread(target=_pipe, args=(u, c), daemon=True).start()\n"
            "            except Exception as e:\n"
            "                print('[PyServer] gh319: bridge disabled', repr(e), flush=True)\n"
            "        threading.Thread(target=_accept_loop, daemon=True).start()\n"
            "    try:\n"
            "        _wjjh_port_bridge()\n"
            "    except Exception:\n"
            "        traceback.print_exc(file=logf)\n"
            "    runpy.run_path(os.path.join(dst, target), run_name='__main__')\n"
            "except BaseException:\n"
            "    traceback.print_exc(file=logf)\n"
            "    logf.write('[PyServer] RUNNER FAILED\\n')\n"
            "    raise\n",
            serverSrc, serverDst];
        int rc = py_run([runner UTF8String]);
        fprintf(stderr, "[PyServer] server exited rc=%d\n", rc);
    }
}

@end
