-- list_thread_messages.applescript — list messages in a shared mailbox folder
-- as a JSON array.
--
-- Usage:
--   osascript list_thread_messages.applescript <mailbox_email> <folder_kind> <days_back> <limit>
--
-- Args:
--   mailbox_email   e.g. "summermed@stanford.edu"
--   folder_kind     "inbox" or "sent"
--   days_back       integer
--   limit           integer cap (script also caps to 5000)
--
-- Resolver model (Outlook 16.108):
--   Outlook does not expose `every exchange account` enumerably here, even
--   though messages carry a reachable `account` reference. So we walk top-
--   level `mail folders`, peek at message 1's account, and match on
--   `email address of (account of message 1) = mailbox_email` AND
--   `name of folder = "Inbox" | "Sent Items"`. Folders with zero messages
--   are skipped (typical for empty side-folders; the real Inbox/Sent Items
--   for an active mailbox always has a first message).
--
-- Per-message JSON keys: message_id, conversation_id, received_at (ISO),
--   sender_name, sender_email, recipients_to, recipients_cc, subject,
--   body_text (uncapped), folder, account.

on run argv
    set mailboxEmail to item 1 of argv
    set folderKind to item 2 of argv
    try
        set daysBack to (item 3 of argv) as integer
    on error
        set daysBack to 365
    end try
    try
        set limitCount to (item 4 of argv) as integer
    on error
        set limitCount to 1000
    end try
    if limitCount > 50000 then set limitCount to 50000

    set targetFolder to my resolveFolder(mailboxEmail, folderKind)
    if targetFolder is missing value then
        return "{\"error\":\"folder_not_found\",\"mailbox\":\"" & mailboxEmail & ¬
            "\",\"kind\":\"" & folderKind & "\"}"
    end if

    -- Outlook can take many minutes to walk a busy folder; the inner `tell`
    -- block defaults to 120s, so wrap it in an explicit larger timeout.
    with timeout of 7200 seconds
        tell application "Microsoft Outlook"
            set cutoff to (current date) - (daysBack * days)
            set totalCount to count of messages of targetFolder

            -- Outlook indexes message 1 = newest, so walking i=1..N is newest-first.
            set collected to {}
            set processed to 0
            set consecutiveOld to 0
            set i to 1

            repeat while (i ≤ totalCount) and (processed < limitCount)
                try
                    set m to message i of targetFolder
                    try
                        set msgDate to time received of m
                    on error
                        set msgDate to missing value
                    end try
                    if (msgDate is not missing value) and (msgDate > cutoff) then
                        set consecutiveOld to 0
                        set end of collected to my serializeMessage(m, mailboxEmail, folderKind)
                        set processed to processed + 1
                    else if msgDate is not missing value then
                        set consecutiveOld to consecutiveOld + 1
                        if consecutiveOld > 500 then exit repeat
                    end if
                on error errMsg
                    -- non-fatal; skip this index
                end try
                set i to i + 1
            end repeat
        end tell
    end timeout

    set output to "["
    repeat with j from 1 to count of collected
        if j > 1 then set output to output & ","
        set output to output & (item j of collected)
    end repeat
    set output to output & "]"
    return output
end run


on resolveFolder(mailboxEmail, folderKind)
    -- folderKind "inbox" / "sent" are aliases for the canonical top-level
    -- folder names; anything else is treated as a literal folder name to
    -- look up among folders owned by `mailboxEmail`.
    if folderKind is "inbox" then
        set targetNames to {"Inbox", "INBOX"}
    else if folderKind is "sent" then
        set targetNames to {"Sent Items", "Sent"}
    else
        set targetNames to {folderKind}
    end if

    tell application "Microsoft Outlook"
        repeat with f in mail folders
            try
                set fname to name of f as string
            on error
                set fname to ""
            end try
            if my listContains(targetNames, fname) then
                try
                    set msgCount to count of messages of f
                on error
                    set msgCount to 0
                end try
                if msgCount > 0 then
                    try
                        set firstMsg to message 1 of f
                        set acctEmail to email address of (account of firstMsg)
                        if acctEmail is mailboxEmail then return f
                    end try
                end if
            end if
        end repeat
    end tell
    return missing value
end resolveFolder


on listContains(lst, target)
    repeat with x in lst
        if (x as string) is target then return true
    end repeat
    return false
end listContains


on serializeMessage(m, mailboxEmail, folderKind)
    tell application "Microsoft Outlook"
        set msgId to id of m as string
        try
            set convId to conversation id of m as string
        on error
            set convId to ""
        end try
        set msgSubject to my jsonEscape(subject of m)
        try
            set bodyRaw to plain text content of m
            set msgBody to my jsonEscape(bodyRaw)
        on error
            set msgBody to ""
        end try
        set receivedISO to my isoDate(time received of m)
        try
            set senderObj to sender of m
            try
                set senderName to my jsonEscape(name of senderObj)
            on error
                set senderName to ""
            end try
            try
                set senderEmail to my jsonEscape(address of senderObj)
            on error
                set senderEmail to ""
            end try
        on error
            set senderName to ""
            set senderEmail to ""
        end try
        try
            set toList to my serializeRecipients(to recipients of m)
        on error
            set toList to "[]"
        end try
        try
            set ccList to my serializeRecipients(cc recipients of m)
        on error
            set ccList to "[]"
        end try
    end tell
    return "{\"message_id\":\"" & msgId & ¬
        "\",\"conversation_id\":\"" & convId & ¬
        "\",\"received_at\":\"" & receivedISO & ¬
        "\",\"sender_name\":\"" & senderName & ¬
        "\",\"sender_email\":\"" & senderEmail & ¬
        "\",\"subject\":\"" & msgSubject & ¬
        "\",\"body_text\":\"" & msgBody & ¬
        "\",\"recipients_to\":" & toList & ¬
        ",\"recipients_cc\":" & ccList & ¬
        ",\"folder\":\"" & folderKind & ¬
        "\",\"account\":\"" & mailboxEmail & "\"}"
end serializeMessage


on serializeRecipients(rList)
    -- Outlook 16 model: a `recipient` (or `to recipient` / `cc recipient`)
    -- has an `email address` PROPERTY which is itself a record with `name`
    -- (display) and `address` (the literal email). Reading `name of r` or
    -- `address of r` directly raises -1700 because the recipient class has
    -- no such direct properties.
    set output to "["
    set idx to 0
    repeat with r in rList
        set idx to idx + 1
        tell application "Microsoft Outlook"
            try
                set ea to email address of r
                try
                    set rName to my jsonEscape(name of ea)
                on error
                    set rName to ""
                end try
                try
                    set rEmail to my jsonEscape(address of ea)
                on error
                    set rEmail to ""
                end try
            on error
                set rName to ""
                set rEmail to ""
            end try
        end tell
        if idx > 1 then set output to output & ","
        set output to output & "{\"name\":\"" & rName & "\",\"email\":\"" & rEmail & "\"}"
    end repeat
    set output to output & "]"
    return output
end serializeRecipients


on jsonEscape(s)
    if s is missing value then return ""
    set txt to s as string
    set txt to my replaceText(txt, "\\", "\\\\")
    set txt to my replaceText(txt, "\"", "\\\"")
    set txt to my replaceText(txt, return, "\\n")
    set txt to my replaceText(txt, linefeed, "\\n")
    set txt to my replaceText(txt, tab, "\\t")
    return txt
end jsonEscape


on replaceText(source, findStr, replaceStr)
    set AppleScript's text item delimiters to findStr
    set pieces to text items of source
    set AppleScript's text item delimiters to replaceStr
    set rejoined to pieces as string
    set AppleScript's text item delimiters to ""
    return rejoined
end replaceText


on isoDate(d)
    if d is missing value then return ""
    set y to year of d as integer
    set mo to month of d as integer
    set da to day of d as integer
    set hh to hours of d as integer
    set mm to minutes of d as integer
    set ss to seconds of d as integer
    return (my pad(y, 4)) & "-" & (my pad(mo, 2)) & "-" & (my pad(da, 2)) & ¬
        "T" & (my pad(hh, 2)) & ":" & (my pad(mm, 2)) & ":" & (my pad(ss, 2)) & "Z"
end isoDate


on pad(n, width)
    set s to n as string
    repeat while (length of s) < width
        set s to "0" & s
    end repeat
    return s
end pad
