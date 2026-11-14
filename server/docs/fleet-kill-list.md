# Starter kill list

The hub hides these packages on a device-owner TV and force-stops them on a rooted TV, on every check-in. The list is editable. Saving a new list replaces this one, and an empty save stays empty.

These are the packages named by the write-ups below. Apps the house actually watches are not on it.

| Package | Why it is here |
|---|---|
| `com.adups.fota` | Preinstalled Adups component. Malwarebytes describes it as a downloader and an information stealer (`adupsfota.apk`, shown as System Update or Wireless Update). [Removal instructions for Adups](https://forums.malwarebytes.com/topic/216616-removal-instructions-for-adups/). |
| `com.adups.fota.sysoper` | The Adups auto-installer that runs with system rights (`FWUpgradeProvider.apk`, shown as UpgradeSys). [Mobile Menace Monday: upping the ante on Adups](https://www.malwarebytes.com/blog/news/2017/12/mobile-menace-monday-upping-the-ante-on-adups-fwupgradeprovider). |
| `com.fw.upgrade.sysoper` | The same UpgradeSys component under its other package name. Same Malwarebytes article. |
| `com.swe.dgbluancher` | The launcher on some Allwinner T95 and Rockchip boxes that loads the preinstalled Corejava payload. [Malwarebytes on the T95](https://www.malwarebytes.com/blog/news/2023/01/preinstalled-malware-infested-t95-tv-box-from-amazon), [DesktopECHO/T95-H616-Malware](https://github.com/DesktopECHO/T95-H616-Malware). The EFF describes the same boxes as sold with botnet malware already on them. |

Adups on a particular box is not always the 2016 build. Malwarebytes has also found a T95 copy that was not doing anything malicious at the time, and removing it can remove that box's only system-update path. It stays on the starter list because the same package name has shipped as a backdoor, and the list is there to edit.
