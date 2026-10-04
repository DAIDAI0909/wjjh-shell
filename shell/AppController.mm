#import "AppController.h"
#import "cocos2d.h"
#import "AppDelegate.h"
#import "RootViewController.h"
#import "PyServer.h"
#import <sys/socket.h>
#import <netinet/in.h>
#import <unistd.h>

@implementation AppController

// cocos 官方模板同款：AppController 里必须有 AppDelegate 实例，
// 否则 Application::getInstance() 返回空指针，initGLContextAttrs 直接 SIGSEGV
static AppDelegate s_sharedApplication;

@synthesize window = _window;
@synthesize viewController = _viewController;

// 轮询 127.0.0.1:7900 直到能连上（私服就绪）或超时
+ (BOOL)waitServerPort:(int)seconds
{
    for (int i = 0; i < seconds * 10; ++i) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port = htons(7900);
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        int rc = connect(fd, (struct sockaddr *)&a, sizeof(a));
        close(fd);
        if (rc == 0) {
            fprintf(stderr, "WJJH_BOOT: private server port ready after %.1fs\n", i * 0.1);
            return YES;
        }
        [NSThread sleepForTimeInterval:0.1];
    }
    fprintf(stderr, "WJJH_BOOT: private server port NOT ready after %ds (continuing anyway)\n", seconds);
    return NO;
}

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions
{
    fprintf(stderr, "WJJH_BOOT: AppController didFinishLaunching\n");

    // 私服最先启动：Python 初始化与后续流程并行。
    // cocos 的 run() 在本方法里跑完整个 main.lua（阻塞主线程），
    // 游戏第一条 HTTP 在 main.lua 里就发出且**不重试** —— 所以主线程必须
    // 等私服端口就绪再放行 cocos（否则 get_time 永远失败，卡「同步时间中」）。
    NSThread *srvThread = [[NSThread alloc] initWithBlock:^{
        NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
        NSString *docsPath = [NSSearchPathForDirectoriesInDomains(
            NSDocumentDirectory, NSUserDomainMask, YES) firstObject];
        fprintf(stderr, "WJJH_BOOT: starting Python private server...\n");
        [PyServer startServerWithBundlePath:bundlePath
                               writablePath:docsPath];
    }];
    srvThread.qualityOfService = NSQualityOfServiceUserInitiated;
    [srvThread start];
    [AppController waitServerPort:5];

    // cocos 官方模板同款：App 里必须有 AppDelegate 实例（文件顶部的 s_sharedApplication），
    // 否则 Application::getInstance() 返回空指针，initGLContextAttrs 直接 SIGSEGV
    cocos2d::Application *app = cocos2d::Application::getInstance();
    app->initGLContextAttrs();

    // 设计分辨率交给 Lua 侧 config.lua（1080x1920 SHOW_ALL），这里给足窗口尺寸
    _window = [[UIWindow alloc] initWithFrame:[[UIScreen mainScreen] bounds]];
    _window.backgroundColor = [UIColor blackColor];

    _viewController = [[RootViewController alloc] initWithNibName:nil bundle:nil];
    _viewController.wantsFullScreenLayout = YES;
    _window.rootViewController = _viewController;
    [_window makeKeyAndVisible];

    return YES;
}

- (void)applicationWillResignActive:(UIApplication *)application
{
    cocos2d::Application::getInstance()->applicationDidEnterBackground();
}

- (void)applicationDidBecomeActive:(UIApplication *)application
{
    cocos2d::Application::getInstance()->applicationWillEnterForeground();
    // PyServer 已在 didFinishLaunching 最先启动（见上）
}

- (void)applicationWillTerminate:(UIApplication *)application
{
}

@end
