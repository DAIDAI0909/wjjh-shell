#import <UIKit/UIKit.h>
#import <stdio.h>
#import <signal.h>
#import <execinfo.h>
#import <string.h>
// gh222-fix: Darwin's top-level <ucontext.h> is guarded by
//   #error The deprecated ucontext routines require _XOPEN_SOURCE to be defined
// so `#import <ucontext.h>` breaks the build (runs 222/223: FAILED shell/main.mm.o
// -> incomplete WJJH.app -> simctl install "Failed to get FD to bundle executable").
// <sys/ucontext.h> is the Darwin-internal header that actually defines
// ucontext_t / mcontext_t, with no such guard. Guard the include so that if the
// header is unavailable the register dump simply degrades (build still succeeds).
#if defined(__APPLE__)
#  if defined(__has_include)
#    if __has_include(<sys/ucontext.h>)
#      import <sys/ucontext.h>
#      define WJJH_HAVE_UCONTEXT 1
#    endif
#  endif
#else
#  import <ucontext.h>
#  define WJJH_HAVE_UCONTEXT 1
#endif

// gh221: 崩溃诊断增强 —— 除了调用栈，还记录 si_addr（触发地址）与 PC/SP。
// 关键用途：区分「空指针解引用」(si_addr 接近 0x0) 与「野指针/越界」(si_addr 为垃圾地址)。
// run 220 的 spine 崩溃只有调用栈，无法判定 Skeleton ctor 是收到 NULL SkeletonData
// 还是索引越界，因此这里补上地址信息。

static void wjjh_dump_regs(void *uap)
{
#if defined(WJJH_HAVE_UCONTEXT) && defined(__APPLE__)
    if (uap == NULL) return;
    ucontext_t *uc = (ucontext_t *)uap;
# if defined(__arm64__)
    fprintf(stderr, "WJJH_REGS: pc=%p lr=%p sp=%p fp=%p\n",
            (void *)uc->uc_mcontext->__ss.__pc,
            (void *)uc->uc_mcontext->__ss.__lr,
            (void *)uc->uc_mcontext->__ss.__sp,
            (void *)uc->uc_mcontext->__ss.__fp);
# elif defined(__x86_64__)
    fprintf(stderr, "WJJH_REGS: rip=%p rsp=%p rbp=%p\n",
            (void *)uc->uc_mcontext->__ss.__rip,
            (void *)uc->uc_mcontext->__ss.__rsp,
            (void *)uc->uc_mcontext->__ss.__rbp);
# else
    (void)uc;
# endif
#else
    (void)uap;
#endif
}

static void wjjh_signal_handler(int sig, siginfo_t *info, void *uap)
{
    fprintf(stderr, "WJJH_SIG: signal %d caught\n", sig);
    if (info != NULL) {
        fprintf(stderr, "WJJH_SIGINFO: si_code=%d si_addr=%p\n",
                (int)info->si_code, info->si_addr);
    }
    wjjh_dump_regs(uap);
    void *bt[128];
    int n = backtrace(bt, 128);
    fprintf(stderr, "WJJH_BT: frames=%d\n", n);
    backtrace_symbols_fd(bt, n, STDERR_FILENO);
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
    {
        struct sigaction sa;
        memset(&sa, 0, sizeof(sa));
        sa.sa_sigaction = wjjh_signal_handler;
        sa.sa_flags = SA_SIGINFO;
        sigemptyset(&sa.sa_mask);
        const int sigs[] = {SIGABRT, SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGTRAP};
        for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); i++)
            sigaction(sigs[i], &sa, NULL);
    }
    NSSetUncaughtExceptionHandler(wjjh_exception_handler);

    @autoreleasepool {
        int retVal = UIApplicationMain(argc, argv, nil, @"AppController");
        fprintf(stderr, "WJJH_BOOT: UIApplicationMain returned %d\n", retVal);
        return retVal;
    }
}
