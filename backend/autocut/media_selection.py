"""Read-only folder selection for the local browser, without uploading videos."""
from pathlib import Path
import json,subprocess,sys,threading
from .file_storage import storage_root,VIDEO_EXTENSIONS

_picker_lock=threading.Lock()
_FOLDER_PANEL_SCRIPT='''
ObjC.import('AppKit');
function folderSelectionResult(response,panel) {
 // JXA exposes NSInteger responses as strings on current macOS.
 return JSON.stringify(Number(response)===Number($.NSModalResponseOK) ? {path:ObjC.unwrap(panel.URL.path)} : {cancelled:true});
}
function run(argv) {
 const app=$.NSApplication.sharedApplication;
 app.setActivationPolicy(1);
 const panel=$.NSOpenPanel.openPanel;
 panel.setCanChooseFiles(false);
 panel.setCanChooseDirectories(true);
 panel.setAllowsMultipleSelection(false);
 panel.setCanCreateDirectories(false);
 panel.setTitle('选择素材文件夹');
 panel.setPrompt('选择');
 panel.setDirectoryURL($.NSURL.fileURLWithPath(argv[0]));
 app.activateIgnoringOtherApps(true);
 const response=panel.runModal;
 return folderSelectionResult(response,panel);
}
'''

_VIDEO_PANEL_SCRIPT='''
ObjC.import('AppKit');
function videoSelectionResult(response,panel) {
 if(Number(response)!==Number($.NSModalResponseOK))return JSON.stringify({cancelled:true});
 const urls=panel.URLs,paths=[];
 for(let i=0;i<Number(urls.count);i++)paths.push(ObjC.unwrap(urls.objectAtIndex(i).path));
 return JSON.stringify({paths:paths});
}
function run(argv) {
 const app=$.NSApplication.sharedApplication;
 app.setActivationPolicy(1);
 const panel=$.NSOpenPanel.openPanel;
 panel.setCanChooseFiles(true);
 panel.setCanChooseDirectories(false);
 panel.setAllowsMultipleSelection(true);
 panel.setCanCreateDirectories(false);
 panel.setAllowedFileTypes(['mp4','mov','m4v','webm']);
 panel.setAllowsOtherFileTypes(false);
 panel.setTitle('选择补充视频');
 panel.setMessage('可多选，最多 5 条视频');
 panel.setPrompt('选择');
 panel.setDirectoryURL($.NSURL.fileURLWithPath(argv[0]));
 app.activateIgnoringOtherApps(true);
 return videoSelectionResult(panel.runModal,panel);
}
'''


def select_videos(paths):
    if not isinstance(paths,list) or not paths or not all(isinstance(p,str) and p.strip() for p in paths):
        raise ValueError('SUPPLEMENT_FILE_SELECTION_INVALID')
    paths=list(dict.fromkeys(paths))
    if len(paths)>5:raise ValueError('SUPPLEMENT_FILE_LIMIT')
    files={}
    for path in paths:
        try:
            p=Path(path).expanduser().resolve(strict=True)
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTENSIONS:
                raise ValueError('SUPPLEMENT_FILE_SELECTION_INVALID')
            size=p.stat().st_size
            if not 0<size<=500*1024**2:raise ValueError('SUPPLEMENT_FILE_SIZE')
        except (FileNotFoundError,PermissionError,OSError):
            raise ValueError('SUPPLEMENT_FILE_UNREADABLE') from None
        files[str(p)]=dict(path=str(p),name=p.name,size=size)
    return dict(paths=list(files),files=list(files.values()))


def pick_videos(initial_path=None):
    """Read-only selection; probing/copying stays in the existing supplement intake."""
    if sys.platform!='darwin':raise ValueError('SUPPLEMENT_PICKER_UNAVAILABLE')
    if not _picker_lock.acquire(blocking=False):raise ValueError('SOURCE_PICKER_BUSY')
    try:
        initial=Path(initial_path).expanduser() if isinstance(initial_path,str) and initial_path else storage_root()/'Supplements'
        if initial.is_file():initial=initial.parent
        if not initial.is_dir():initial=Path.home()
        response=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',_VIDEO_PANEL_SCRIPT,str(initial)],capture_output=True,text=True,timeout=300,check=False)
        if response.returncode:raise ValueError('SUPPLEMENT_PICKER_UNAVAILABLE')
        try:result=json.loads(response.stdout)
        except (ValueError,TypeError):raise ValueError('SUPPLEMENT_PICKER_UNAVAILABLE') from None
        if not isinstance(result,dict):raise ValueError('SUPPLEMENT_PICKER_UNAVAILABLE')
        if result.get('cancelled'):return dict(cancelled=True)
        return select_videos(result.get('paths'))
    except subprocess.TimeoutExpired:
        raise ValueError('SOURCE_PICKER_TIMEOUT') from None
    except OSError:
        raise ValueError('SUPPLEMENT_PICKER_UNAVAILABLE') from None
    finally:_picker_lock.release()


def pick_folder(initial_path=None):
    """Show macOS's real folder chooser from the authenticated local backend."""
    if sys.platform!='darwin':raise ValueError('SOURCE_PICKER_UNAVAILABLE')
    if not _picker_lock.acquire(blocking=False):raise ValueError('SOURCE_PICKER_BUSY')
    try:
        initial=Path(initial_path).expanduser() if isinstance(initial_path,str) and initial_path else storage_root()/'Media'
        if not initial.is_dir():initial=Path.home()
        response=subprocess.run(['/usr/bin/osascript','-l','JavaScript','-e',_FOLDER_PANEL_SCRIPT,str(initial)],capture_output=True,text=True,timeout=300,check=False)
        if response.returncode:raise ValueError('SOURCE_PICKER_UNAVAILABLE')
        try:result=json.loads(response.stdout)
        except (ValueError,TypeError):raise ValueError('SOURCE_PICKER_UNAVAILABLE') from None
        if not isinstance(result,dict):raise ValueError('SOURCE_PICKER_UNAVAILABLE')
        if result.get('cancelled'):return dict(cancelled=True)
        return select_folder(result.get('path'))
    except subprocess.TimeoutExpired:
        raise ValueError('SOURCE_PICKER_TIMEOUT') from None
    except OSError:
        raise ValueError('SOURCE_PICKER_UNAVAILABLE') from None
    finally:_picker_lock.release()


def folder_info(path=None):
    if path is None:
        default=storage_root()/'Media'
        path=default if default.is_dir() else Path.home()
    try:
        folder=Path(path).expanduser().resolve(strict=True)
        if not folder.is_dir():raise ValueError('SOURCE_NOT_FOLDER')
        directories=[];count=0
        # Only list the selected folder; never recurse through user media.
        for child in folder.iterdir():
            if child.name.startswith('.'):continue
            if child.is_dir():
                directories.append(dict(name=child.name,path=str(child)))
            elif child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                count+=1
        directories.sort(key=lambda row:row['name'].casefold())
        return dict(path=str(folder),display_path=str(folder),
                    parent=str(folder.parent) if folder.parent!=folder else None,
                    directories=directories,video_count=count)
    except PermissionError:
        raise ValueError('SOURCE_FOLDER_PERMISSION') from None
    except FileNotFoundError:
        raise ValueError('SOURCE_FOLDER_MISSING') from None
    except OSError:
        raise ValueError('SOURCE_FOLDER_UNREADABLE') from None


def select_folder(path):
    if not isinstance(path,str) or not path.strip():raise ValueError('SOURCE_FOLDER_REQUIRED')
    result=folder_info(path)
    if not result['video_count']:raise ValueError('NO_VALID_SOURCE')
    return {key:result[key] for key in ('path','display_path','video_count')}
