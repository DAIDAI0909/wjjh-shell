#import <UIKit/UIKit.h>
#import <stdio.h>
#import <signal.h>

static void wjjh_signal_handler(int sig)
{
    fprintf(stderr, "WJJH_SIG: signal %d caught\n", sig);
    fflush(stderr);
    signal(sig, SIG_DFL);
    raise(sig);
}

static void wjjh_exception_handler(NSException *exc)
{
    fprintf(stderr, "WJJH_EXC: %s (%s)\n%s\n",
            exc.name.UTF8String, exc.reason.UTF8String,
            exc.callStackSymbols.description.UTF8String);
    fflush(stderr);
}

int main(int argc, char *argv[])
{
    // CI 诊断：分离启动模式下 stderr/stdout 无人接收；
    // freopen 到各 CI 传入的 launch.log（CMake 的 WJJH_LAUNCH_LOG 编译期注入）。
    // 真机/本地无此路径时 freopen 失败，行为不变。
#ifdef WJJH_LAUNCH_LOG_PATH
    if (freopen(WJJH_LAUNCH_LOG_PATH, "a", stderr))
        setvbuf(stderr, NULL, _IOLBF, 0);
    freopen(WJJH_LAUNCH_LOG_PATH, "a", stdout);
#endif

    fprintf(stderr, "WJJH_BOOT: main entered\n");
    for (int i = 0; i < 32; i++)
        if (i == SIGABRT || i == SIGSEGV || i == SIGBUS || i == SIGILL || i == SIGFPE || i == SIGTRAP)
            signal(i, wjjh_signal_handler);
    NSSetUncaughtExceptionHandler(wjjh_exception_handler);

    @autoreleasepool {
        int retVal = UIApplicationMain(argc, argv, nil, @"AppController");
        fprintf(stderr, "WJJH_BOOT: UIApplicationMain returned %d\n", retVal);
        return retVal;
    }
}
