#import "PyServer.h"
#import <dlfcn.h>
#import <Foundation/Foundation.h>

@implementation PyServer

+ (void)startServerWithBundlePath:(NSString *)bundlePath
                     writablePath:(NSString *)writablePath {
    @autoreleasepool {
        NSString *fwPath = [bundlePath stringByAppendingPathComponent:@"Frameworks/Python.framework"];
        NSString *dylib = [fwPath stringByAppendingPathComponent:@"Python"];
        NSString *pyHome = [bundlePath stringByAppendingPathComponent:@"pyhome"];
        NSString *serverSrc = [bundlePath stringByAppendingPathComponent:@"server/app"];
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
