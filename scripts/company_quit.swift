import Cocoa
let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.autocut.desktop")
if CommandLine.arguments.contains("--check") { exit(apps.isEmpty ? 0 : 1) }
for app in apps { if !app.terminate() { exit(1) } }
