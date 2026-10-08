// Rasterize the single SVG brand asset into the standard macOS icon sizes.
// No UI, screenshots, system font, or production application changes.
import Cocoa
let args=CommandLine.arguments
guard args.count==3,let source=NSImage(contentsOfFile:args[1]) else {fatalError("ICON_SOURCE_INVALID")}
let folder=URL(fileURLWithPath:args[2],isDirectory:true)
try FileManager.default.createDirectory(at:folder,withIntermediateDirectories:true)
for size in [16,32,128,256,512] {
 for factor in [1,2] {
  let pixels=size*factor
  guard let bitmap=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:pixels,pixelsHigh:pixels,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0),let context=NSGraphicsContext(bitmapImageRep:bitmap) else {fatalError("ICON_RENDER_FAILED")}
  NSGraphicsContext.saveGraphicsState();NSGraphicsContext.current=context;context.imageInterpolation = .high
  source.draw(in:NSRect(x:0,y:0,width:pixels,height:pixels),from:.zero,operation:.copy,fraction:1)
  NSGraphicsContext.restoreGraphicsState()
  guard let data=bitmap.representation(using:.png,properties:[:]) else {fatalError("ICON_PNG_FAILED")}
  try data.write(to:folder.appendingPathComponent("icon_\(size)x\(size)\(factor==2 ? "@2x" : "").png"))
 }
}
print("AutoCut icon sizes generated")
