// Empty

#include <TargetConditionals.h>
#if TARGET_OS_SIMULATOR
/* Thomas Creek: Go 1.26's cgo runtime for the iOS simulator calls these lldb exception-port hooks,
   which nothing defines there. They are only needed under a debugger on a device. */
void darwin_arm_init_mach_exception_handler(void) {}
void darwin_arm_init_thread_exception_port(void) {}
#endif
