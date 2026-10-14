#import <UIKit/UIKit.h>
#import <stdio.h>
#import <stdlib.h>
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

// 极早期探针：__attribute__((constructor)) 比 main() 更早运行（dyld 完成加载、
// 进入静态初始化阶段后）。用途——把「崩在 main() 之前」与「崩在 main() 之后」
// 彻底分开：
//   * premain.log 缺失  => dyld 根本没成功加载本可执行文件/依赖库（符号缺失、
//     arm64 平台戳不符、签名校验失败等「库/签名层」问题）；
//   * premain.log 存在  => dyld 加载 OK，崩溃发生在 main() 之后（我们的启动代码）。
// 纯 C 实现，不依赖 stderr（真机无调试器时 stderr 被系统丢弃），直接写沙盒。
static const char *wjjh_sandbox_path(const char *name, char *buf, size_t n)
{
    const char *home = getenv("HOME");
    if (home != NULL && home[0] != '\0')
        snprintf(buf, n, "%s/Documents/%s", home, name);
    else
        snprintf(buf, n, "/tmp/%s", name);
    return buf;
}

static void wjjh_early_signal_handler(int sig, siginfo_t *info, void *uap)
{
    (void)uap;
    char path[1024];
    wjjh_sandbox_path("premain.log", path, sizeof(path));
    FILE *f = fopen(path, "a");
    if (f != NULL) {
        fprintf(f, "WJJH_EARLY_SIG: signal %d si_addr=%p\n",
                sig, info != NULL ? info->si_addr : NULL);
        fclose(f);
    }
    signal(sig, SIG_DFL);
    raise(sig);
}

__attribute__((constructor))
static void wjjh_premain_probe(void)
{
    char path[1024];
    wjjh_sandbox_path("premain.log", path, sizeof(path));
    FILE *f = fopen(path, "a");
    if (f != NULL) {
        fprintf(f, "WJJH_PREMAIN: constructor ran (dyld load OK)\n");
        fclose(f);
    }
    // 静态初始化阶段就装好崩溃 handler，捕获 main() 之前的信号崩溃。
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = wjjh_early_signal_handler;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    const int sigs[] = {SIGABRT, SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGTRAP};
    for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); i++)
        sigaction(sigs[i], &sa, NULL);
}

int main(int argc, char *argv[])
{
    // 诊断日志落盘：CI 传入路径优先；真机/本地该路径不存在时 freopen 失败，
    // 而 iOS 在无调试器连接时会丢弃 App 的 stderr，故回退写到 App 沙盒
    // Documents/launch.log，这样真机崩溃诊断(WJJH_SIG/WJJH_BT/WJJH_EXC)可经
    // 爱思文件管理从沙盒取出，不再依赖被丢弃的 stderr。
    const char *wjjh_log_path = NULL;
#ifdef WJJH_LAUNCH_LOG_PATH
    wjjh_log_path = WJJH_LAUNCH_LOG_PATH;
#endif
    if (wjjh_log_path == NULL || freopen(wjjh_log_path, "a", stderr) == NULL) {
        @autoreleasepool {
            NSArray *docs = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory,
                                                                 NSUserDomainMask, YES);
            if ([docs count] > 0) {
                NSString *sb = [docs.firstObject stringByAppendingPathComponent:@"launch.log"];
                freopen([sb UTF8String], "a", stderr);
                freopen([sb UTF8String], "a", stdout);
            }
        }
    } else {
        freopen(wjjh_log_path, "a", stdout);
    }
    setvbuf(stderr, NULL, _IOLBF, 0);

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
