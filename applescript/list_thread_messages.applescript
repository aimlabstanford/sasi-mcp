-- list_thread_messages.applescript — list messages in a shared mailbox folder
-- as a JSON array. Single new script combining patterns from
-- ledger-bridge's list_messages.applescript (account resolver, date cutoff)
-- and list_unread.applescript (reverse-index walk that avoids `whose` timeout
-- on busy folders, JSON-emit helpers).
--
-- Usage:
--   osascript list_thread_messages.applescript <mailbox_email> <folder_kind> <days_back> <limit>
--
-- Args:
--   mailbox_email   e.g. "summermed@stanford.edu"
--   folder_kind     "inbox" or "sent"
--   days_back       integer, e.g. 365
--   limit           integer cap on returned messages (script also caps to 5000)
--
-- Output: JSON array of {message_id, conversation_id, received_at (ISO),
--   sender_name, sender_email, recipients_to, recipients_cc, subject,
--   body_text (uncapped), folder, account}.
--
-- Resolver order:
--   (a) inbox/sent items folder of the exchange account whose email matches.
--   (b) walk top-level mail folders for one whose name matches the shared
--       mailbox display name, then descend into its Inbox / Sent Items child.

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
    if limitCount > 5000 then set limitCount to 5000

    tell application "Microsoft Outlook"
        set targetFolder to my resolveFolder(mailboxEmail, folderKind)
        if targetFolder is missing value then
            return "{\"error\":\"folder_not_found\",\"mailbox\":\"" & mailboxEmail & ¬
                "\",\"kind\":\"" & folderKind & "\"}"
        end if

        set cutoff to (current date) - (daysBack * days)
        set totalCount to count of messages of targetFolder

        -- Outlook indexes message 1 = newest, so walking i=1..N is newest-first.
        -- Stop early once we cross the cutoff (with a tolerance window in case
        -- the order is occasionally non-monotonic on shared mailboxes).
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

    set output to "["
    repeat with j from 1 to count of collected
        if j > 1 then set output to output & ","
        set output to output & (item j of collected)
    end repeat
    set output to output & "]"
    return output
end run


on resolveFolder(mailboxEmail, folderKind)
    tell application "Microsoft Outlook"
        -- Path A: full Exchange account whose email matches.
        repeat with a in exchange accounts
            try
                set aemail to email address of a
                if aemail is mailboxEmail then
                    if folderKind is "sent" then
                        return sent items folder of a
                    else
                        return inbox of a
                    end if
                end if
            end try
        end repeat

        -- Path B: top-level shared-mailbox folder under the user's account.
        -- Outlook surfaces shared mailboxes either as a top-level mail folder
        -- whose name matches the shared display name, or as a child under the
        -- delegate account. Try the top level first.
        set sharedName to my localPart(mailboxEmail)
        repeat with f in mail folders
            try
                set fname to name of f as string
                if (fname contains sharedName) or (fname contains mailboxEmail) then
                    if folderKind is "sent" then
                        return my findChild(f, {"Sent Items", "Sent"})
                    else
                        return my findChild(f, {"Inbox"})
                    end if
                end if
            end try
        end repeat
    end tell
    return missing value
end resolveFolder


on findChild(parent, candidateNames)
    tell application "Microsoft Outlook"
        repeat with cn in candidateNames
            try
                set candidate to folder (cn as string) of parent
                return candidate
            end try
        end repeat
    end tell
    return missing value
end findChild


on localPart(email)
    set AppleScript's text item delimiters to "@"
    set parts to text items of email
    set AppleScript's text item delimiters to ""
    if (count of parts) > 0 then return item 1 of parts
    return email
end localPart


on serializeMessage(m, mailboxEmail, folderKind)
    tell application "Microsoft Outlook"
        set msgId to id of m as string
        set msgSubject to my jsonEscape(subject of m)
        try
            set convId to conversation id of m as string
        on error
            set convId to ""
        end try
        try
            set bodyRaw to plain text content of m
            -- NO 2000-char cap. We need the full message for Q&A canonicalization.
            set msgBody to my jsonEscape(bodyRaw)
        on error
            set msgBody to ""
        end try
        set receivedISO to my isoDate(time received of m)
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
        set toList to my serializeRecipients(to recipients of m)
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
    set output to "["
    set idx to 0
    repeat with r in rList
        set idx to idx + 1
        tell application "Microsoft Outlook"
            try
                set rName to my jsonEscape(name of r)
            on error
                set rName to ""
            end try
            try
                set rEmail to my jsonEscape(address of r)
            on error
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
