#import "RootViewController.h"
#import "cocos2d.h"
#import "AppDelegate.h"
#import "platform/ios/CCEAGLView-ios.h"

@implementation RootViewController

+ (Class)layerClass
{
    return [CAEAGLLayer class];
}

- (void)loadView
{
    self.view = [[UIView alloc] initWithFrame:[[UIScreen mainScreen] bounds]];
}

- (void)viewDidLoad
{
    [super viewDidLoad];

    fprintf(stderr, "WJJH_BOOT: RootViewController viewDidLoad\n");
    // 官方模板模式：EAGLView 必须由 CAEAGLLayer 承载，普通 UIView 画不了 GL
    CCEAGLView *eaglView = [CCEAGLView viewWithFrame:[UIScreen mainScreen].bounds
                                         pixelFormat:kEAGLColorFormatRGBA8
                                         depthFormat:GL_DEPTH24_STENCIL8_OES
                                  preserveBackbuffer:NO
                                          sharegroup:nil
                                       multiSampling:NO
                                     numberOfSamples:0];
    [eaglView setMultipleTouchEnabled:YES];
    self.view = eaglView;

    cocos2d::GLViewImpl *glview = cocos2d::GLViewImpl::createWithEAGLView((__bridge void *)eaglView);
    cocos2d::Director::getInstance()->setOpenGLView(glview);

    cocos2d::Application::getInstance()->run();
}

- (BOOL)shouldAutorotateToInterfaceOrientation:(UIInterfaceOrientation)interfaceOrientation
{
    return UIInterfaceOrientationIsPortrait(interfaceOrientation);
}

- (BOOL)shouldAutorotate
{
    return NO;
}

- (UIInterfaceOrientationMask)supportedInterfaceOrientations
{
    return UIInterfaceOrientationMaskPortrait;
}

- (void)didReceiveMemoryWarning
{
    [super didReceiveMemoryWarning];
}

@end
