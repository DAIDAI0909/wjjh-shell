#import <Foundation/Foundation.h>
#import <dlfcn.h>

NS_ASSUME_NONNULL_BEGIN

/// iOS 内嵌 Python 私服启动器（dlopen 运行时加载，零编译期依赖）
/// 在 App 启动后初始化 Python 解释器，在后台线程运行 jhserver（监听 127.0.0.1:7900）
@interface PyServer : NSObject

/// 启动 Python 私服（阻塞当前线程，应从 dispatch_async 调用）
+ (void)startServerWithBundlePath:(NSString *)bundlePath
                     writablePath:(NSString *)writablePath;

@end

/// Python C API 函数指针（dlsym 加载）
typedef void (*Py_SetHome_fn)(const char *);
typedef void (*Py_Init_fn)(void);
typedef int  (*Py_Run_fn)(const char *);
typedef const char * (*Py_Ver_fn)(void);

NS_ASSUME_NONNULL_END
