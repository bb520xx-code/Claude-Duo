"""Ejecting a drive (Finder's ⏏, or `diskutil eject`) while grid sessions work on it.

macOS's Disk Arbitration asks every app that registered an "unmount approval" callback before it unmounts a
volume. Claude Duo's callback parks the grid panes on that drive (sessions.release_drive), so nothing holds the
drive and the eject goes through; the panes resume by themselves when it's plugged back in. Mac only.
Called through ctypes, so it needs no extra package.
"""
import ctypes

import objc

_keep = []  # the session and the callback must live as long as the app, or macOS would call freed memory


def on_eject(handler):
    """Run handler(volume_path) on the main thread just before any volume is unmounted. It must not take long:
    the eject waits for it. The eject always goes ahead (Claude Duo never objects)."""
    vp = ctypes.c_void_p
    da = ctypes.CDLL("/System/Library/Frameworks/DiskArbitration.framework/DiskArbitration")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    da.DASessionCreate.restype, da.DASessionCreate.argtypes = vp, [vp]
    da.DADiskCopyDescription.restype, da.DADiskCopyDescription.argtypes = vp, [vp]
    cf.CFRunLoopGetMain.restype, cf.CFRunLoopGetMain.argtypes = vp, []
    cf.CFRelease.restype, cf.CFRelease.argtypes = None, [vp]
    approval = ctypes.CFUNCTYPE(vp, vp, vp)  # DADissenterRef (*)(DADiskRef disk, void *context)
    da.DARegisterDiskUnmountApprovalCallback.restype = None
    da.DARegisterDiskUnmountApprovalCallback.argtypes = [vp, vp, approval, vp]
    da.DASessionScheduleWithRunLoop.restype = None
    da.DASessionScheduleWithRunLoop.argtypes = [vp, vp, vp]
    key = objc.objc_object(c_void_p=vp.in_dll(da, "kDADiskDescriptionVolumePathKey").value)
    modes = vp.in_dll(cf, "kCFRunLoopCommonModes").value  # also while a menu is open

    def approve(disk, _context):
        try:
            desc = da.DADiskCopyDescription(disk)
            if desc:
                url = objc.objc_object(c_void_p=desc).get(key)  # CFDictionary is an NSDictionary
                cf.CFRelease(desc)
                path = url.path() if url is not None else None
                if path:
                    handler(str(path))
        except Exception:
            pass
        return None  # no objection

    callback = approval(approve)
    session = da.DASessionCreate(None)
    if not session:
        return False
    da.DARegisterDiskUnmountApprovalCallback(session, None, callback, None)
    da.DASessionScheduleWithRunLoop(session, cf.CFRunLoopGetMain(), modes)
    _keep.extend([callback, session, da, cf])
    return True
