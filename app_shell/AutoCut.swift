import Cocoa
import WebKit
import UniformTypeIdentifiers

final class AppDelegate:NSObject,NSApplicationDelegate,WKScriptMessageHandler,WKNavigationDelegate,NSWindowDelegate {
 var window:NSWindow!;var view:WKWebView!;var process:Process?;var port=0;var token="";var poll:Timer?;var completionPoll:Timer?;var quitWhenIdle=false
 let home=ProcessInfo.processInfo.environment["AUTOCUT_DATA_HOME"].map { URL(fileURLWithPath:$0) } ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AutoCut")
 var resources:URL {Bundle.main.resourceURL!}
 func applicationDidFinishLaunching(_ note:Notification){
  NSApp.setActivationPolicy(.regular)
  if let icon=NSImage(contentsOf:resources.appendingPathComponent("CreativeFactory.icns")){NSApp.applicationIconImage=icon}
  let menu=NSMenu();let item=NSMenuItem();menu.addItem(item);let sub=NSMenu();sub.addItem(withTitle:"关于 Creative Factory",action:#selector(about),keyEquivalent:"");sub.addItem(.separator());sub.addItem(withTitle:"退出 Creative Factory",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q");item.submenu=sub
  let edit=NSMenuItem(title:"编辑",action:nil,keyEquivalent:"");let e=NSMenu(title:"编辑");e.addItem(withTitle:"复制",action:#selector(NSText.copy(_:)),keyEquivalent:"c");e.addItem(withTitle:"粘贴",action:#selector(NSText.paste(_:)),keyEquivalent:"v");e.addItem(withTitle:"全选",action:#selector(NSText.selectAll(_:)),keyEquivalent:"a");edit.submenu=e;menu.addItem(edit);NSApp.mainMenu=menu
  window=NSWindow(contentRect:NSRect(x:0,y:0,width:1440,height:900),styleMask:[.titled,.closable,.miniaturizable,.resizable],backing:.buffered,defer:false);window.title="Creative Factory";window.minSize=NSSize(width:1060,height:700);window.center();window.delegate=self
  let cfg=WKWebViewConfiguration();cfg.userContentController.add(self,name:"native");cfg.websiteDataStore = .nonPersistent()
  view=WKWebView(frame:.zero,configuration:cfg);view.navigationDelegate=self;window.contentView=view
  view.loadHTMLString("<body style='font:13px -apple-system;background:#f7f7f8;color:#858995;display:grid;place-items:center;height:95vh'><div>正在启动 Creative Factory…</div></body>",baseURL:nil)
  window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true);connectOrStart()
 }
 func state()->[String:Any]?{guard let d=try? Data(contentsOf:home.appendingPathComponent("runtime/backend.json")),let obj=try? JSONSerialization.jsonObject(with:d) as? [String:Any] else{return nil};return obj}
 func connectOrStart(){
  if let s=state(),let p=s["port"] as? Int,let t=s["token"] as? String {
   port=p;token=t
   call("/health"){d in if d?["status"] as? String=="PASS" {self.showUI()}else{self.startBackend()}}
  }else{startBackend()}
 }
 func startBackend(){
  do{
   try FileManager.default.createDirectory(at:home.appendingPathComponent("logs"),withIntermediateDirectories:true)
   let p=Process();p.executableURL=resources.appendingPathComponent("runtime/python/bin/python3.12");p.arguments=["-B","-m","autocut.server"];p.currentDirectoryURL=home
   var env=ProcessInfo.processInfo.environment;env["AUTOCUT_RESOURCES"]=resources.path;env["AUTOCUT_DATA_HOME"]=home.path;env["PYTHONPATH"]=resources.appendingPathComponent("backend").path+":"+resources.appendingPathComponent("runtime/main").path;env["PYTHONDONTWRITEBYTECODE"]="1";env["PYTHONNOUSERSITE"]="1";env["PATH"]=resources.appendingPathComponent("runtime/media/bin").path+":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
   for key in Array(env.keys) where key.contains("API_KEY") || key.contains("AUTHORIZATION"){env.removeValue(forKey:key)}
   p.environment=env
   let log=home.appendingPathComponent("logs/backend.log");if !FileManager.default.fileExists(atPath:log.path){FileManager.default.createFile(atPath:log.path,contents:nil)};let handle=try FileHandle(forWritingTo:log);try handle.seekToEnd();p.standardOutput=handle;p.standardError=handle
   try p.run();process=p
   var tries=0
   poll=Timer.scheduledTimer(withTimeInterval:0.4,repeats:true){timer in
    tries+=1
    if let s=self.state(),let p=s["port"] as? Int,let t=s["token"] as? String {
     self.port=p;self.token=t
     self.call("/health"){d in if d?["status"] as? String=="PASS" {timer.invalidate();self.showUI()}}
    }
    if tries>100 {timer.invalidate();self.view.loadHTMLString("<body style='font:13px -apple-system;padding:50px'>Creative Factory 未能完成启动。请查看本机日志，或联系管理员。</body>",baseURL:nil)}
   }
  }catch{showError("无法启动剪辑服务")}
 }
 func showUI(){
  let cookie=HTTPCookie(properties:[.domain:"127.0.0.1",.path:"/",.name:"autocut_session",.value:token,.init("HttpOnly"):"TRUE",.init("SameSite"):"Strict"])!
  view.configuration.websiteDataStore.httpCookieStore.setCookie(cookie){self.view.load(URLRequest(url:URL(string:"http://127.0.0.1:\(self.port)/")!))}
  if completionPoll==nil {completionPoll=Timer.scheduledTimer(withTimeInterval:4,repeats:true){_ in self.completedOutputs()}}
 }
 func completedOutputs(){
  call("/runtime/completions",body:[:]){d in
   guard let events=d?["events"] as? [[String:Any]] else{return}
   for event in events {
    guard event["open_output"] as? Bool == true,let path=event["output_dir"] as? String else{continue}
    let u=URL(fileURLWithPath:path).standardizedFileURL
    guard u.lastPathComponent=="outputs",FileManager.default.fileExists(atPath:u.path) else{continue}
    NSWorkspace.shared.open(u)
   }
  }
 }
 func call(_ path:String,body:[String:Any]?=nil,completion:@escaping([String:Any]?)->Void){
  guard let u=URL(string:"http://127.0.0.1:\(port)\(path)") else {completion(nil);return}
  var r=URLRequest(url:u);r.timeoutInterval=15;r.setValue("Bearer "+token,forHTTPHeaderField:"Authorization")
  if let body=body{r.httpMethod="POST";r.setValue("application/json",forHTTPHeaderField:"Content-Type");r.httpBody=try? JSONSerialization.data(withJSONObject:body)}
  URLSession.shared.dataTask(with:r){d,res,error in let obj=d.flatMap{try? JSONSerialization.jsonObject(with:$0) as? [String:Any]};DispatchQueue.main.async{completion(obj)}}.resume()
 }
 func reply(_ id:String,_ data:Any?,_ error:String?=nil){
  let a:[Any]=[id,data ?? NSNull(),error as Any? ?? NSNull()]
  if let d=try? JSONSerialization.data(withJSONObject:a),let s=String(data:d,encoding:.utf8){view.evaluateJavaScript("window.nativeReply(...\(s))",completionHandler:nil)}
 }
 func userContentController(_ userContentController:WKUserContentController,didReceive message:WKScriptMessage){
  guard message.frameInfo.isMainFrame,message.frameInfo.securityOrigin.host=="127.0.0.1",message.frameInfo.securityOrigin.port==port,let b=message.body as? [String:Any],let id=b["id"] as? String,let action=b["action"] as? String else{return}
  if ["folder","reference","music","supplement"].contains(action) {
   let panel=NSOpenPanel();panel.canChooseDirectories=action=="folder";panel.canChooseFiles=action != "folder";panel.allowsMultipleSelection=false;panel.prompt=action=="folder" ? "选择素材目录" : "添加文案表格"
   if action=="music"{panel.prompt="选择音乐";panel.allowedContentTypes=[UTType.mp3,UTType.wav,UTType(filenameExtension:"m4a")!]}
   if action=="supplement"{panel.prompt="选择补充视频";panel.allowedContentTypes=[UTType.mpeg4Movie,UTType.quickTimeMovie,UTType(filenameExtension:"m4v")!,UTType(filenameExtension:"webm")!]}
   if action=="reference"{panel.allowedContentTypes=[UTType(filenameExtension:"csv")!,UTType(filenameExtension:"xlsx")!,UTType(filenameExtension:"xls")!]}
   panel.beginSheetModal(for:window){response in
    guard response == .OK,let u=panel.url else{self.reply(id,nil);return}
    self.call("/native/grant",body:["path":u.path]){d in self.reply(id,d)}
   }
  } else if action=="reveal",let p=b["path"] as? String {
   // Only existing output files; source folders selected by the user are never altered.
   let u=URL(fileURLWithPath:p).standardizedFileURL
   guard u.isFileURL,FileManager.default.fileExists(atPath:u.path),["mp4","mov"].contains(u.pathExtension.lowercased()),u.deletingLastPathComponent().lastPathComponent=="outputs" else{reply(id,nil,"文件不可用");return}
   NSWorkspace.shared.activateFileViewerSelecting([u]);reply(id,["ok":true])
  }
 }
 func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
  guard let u=action.request.url else{decisionHandler(.cancel);return}
  decisionHandler((u.scheme=="about" || (u.scheme=="http" && u.host=="127.0.0.1" && u.port==port)) ? .allow : .cancel)
 }
 func applicationShouldHandleReopen(_ sender:NSApplication,hasVisibleWindows flag:Bool)->Bool{window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true);return true}
 func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{return false}
 func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply{
  call("/runtime/active"){d in
   let count=d?["count"] as? Int ?? 0
   if count>0 {
    let a=NSAlert();a.messageText="仍有 \(count) 个任务正在运行";a.informativeText="保持后台运行可继续剪辑。停止时会等待当前步骤完成并保存进度。";a.addButton(withTitle:"保持后台运行");a.addButton(withTitle:"停止并退出")
    if a.runModal() == .alertFirstButtonReturn{self.window.orderOut(nil);sender.reply(toApplicationShouldTerminate:false)}
    else{self.call("/runtime/stop-all",body:[:]){_ in self.awaitStopped(sender)}}
   }else{self.call("/runtime/shutdown",body:[:]){_ in sender.reply(toApplicationShouldTerminate:true)}}
  };return .terminateLater
 }
 func awaitStopped(_ sender:NSApplication){
  call("/runtime/active"){d in
   if (d?["count"] as? Int ?? 0)==0 {self.call("/runtime/shutdown",body:[:]){_ in sender.reply(toApplicationShouldTerminate:true)}}
   else{DispatchQueue.main.asyncAfter(deadline:.now()+1){self.awaitStopped(sender)}}
  }
 }
 @objc func about(){let a=NSAlert();a.messageText="Creative Factory";a.informativeText="版本 \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0")\nBuild \(Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "")\n本机自动剪辑";a.runModal()}
 func showError(_ s:String){let a=NSAlert();a.messageText=s;a.runModal()}
}
let app=NSApplication.shared;let delegate=AppDelegate();app.delegate=delegate;app.run()
