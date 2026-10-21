"""Windows desktop notifications, with no extra packages.

Windows PowerShell can reach Windows' own notification system (the WinRT
ToastNotificationManager), so a short PowerShell script shows the
notification. The title and message are passed as environment variables, not
pasted into the script, so quotes or odd characters in them can't break it.

    python notify.py "Title" "Message"      to try it
"""

import os
import subprocess
import sys

# Notifications have to come "from" an installed app. PowerShell's own app ID
# is always there, so the notification shows as coming from Windows PowerShell.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

SCRIPT = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $xml.GetElementsByTagName('text')
$text.Item(0).AppendChild($xml.CreateTextNode($env:NOTIFY_TITLE)) | Out-Null
$text.Item(1).AppendChild($xml.CreateTextNode($env:NOTIFY_MESSAGE)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{POWERSHELL_APP_ID}').Show($toast)
"""


def notify(title: str, message: str) -> bool:
    """Show a Windows notification. Returns False if it couldn't (never raises)."""
    env = {**os.environ, "NOTIFY_TITLE": title, "NOTIFY_MESSAGE": message}
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            env=env, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "Job Sourcer"
    message = sys.argv[2] if len(sys.argv) > 2 else "Test notification"
    print("shown" if notify(title, message) else "failed")
