import Foundation
import Security
let allowed:Set<String>=["OPENAI_API_KEY","DEEPSEEK_API_KEY","ELEVENLABS_API_KEY","AUTOCUT_UPGRADE_TEST"]
let data=FileHandle.standardInput.readDataToEndOfFile()
do {
 let request=try JSONSerialization.jsonObject(with:data) as! [String:String]
 let account=request["account"] ?? ""
 guard allowed.contains(account) else {exit(2)}
 let service="com.autocut.providers"
 let query:[String:Any]=[kSecClass as String:kSecClassGenericPassword,kSecAttrService as String:service,kSecAttrAccount as String:account]
 var result:[String:Any]=[:]
 switch request["operation"] {
 case "set":
  let secret=(request["value"] ?? "").data(using:.utf8)!
  var status=SecItemUpdate(query as CFDictionary,[kSecValueData as String:secret] as CFDictionary)
  if status==errSecItemNotFound {
   var add=query;add[kSecValueData as String]=secret;add[kSecAttrAccessible as String]=kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
   status=SecItemAdd(add as CFDictionary,nil)
  }
  guard status==errSecSuccess else {exit(3)};result=["configured":true]
 case "delete":
  let status=SecItemDelete(query as CFDictionary);guard status==errSecSuccess || status==errSecItemNotFound else {exit(3)};result=["configured":false]
 case "get", "status":
  var q=query;q[kSecReturnData as String]=true;q[kSecMatchLimit as String]=kSecMatchLimitOne
  var value:CFTypeRef?;let status=SecItemCopyMatching(q as CFDictionary,&value)
  if status==errSecItemNotFound {result=["configured":false,"value":""]}
  else {guard status==errSecSuccess,let d=value as? Data else {exit(3)};result=["configured":!d.isEmpty];if request["operation"]=="get" {result["value"]=String(data:d,encoding:.utf8) ?? ""}}
 default:exit(2)
 }
 FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:result))
} catch {exit(2)}
