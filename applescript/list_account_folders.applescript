-- list_account_folders.applescript — discover every top-level mail folder
-- belonging to a given Outlook account, by looking at the account on
-- message 1 of each folder.
--
-- Usage: osascript list_account_folders.applescript <mailbox_email>
-- Output: JSON array of {name, message_count} for non-empty folders.
--
-- Why this is needed: Outlook 16 won't let us enumerate `every account`
-- (-1728), and `every mail folder of <account>` is also blocked. The only
-- reliable handle on "which folders does summermed@ own?" is to peek at
-- message 1's account property per folder. Folders with zero messages
-- can't be probed this way; they're omitted (and aren't useful anyway).

on run argv
    set mailboxEmail to item 1 of argv
    set collected to {}

    with timeout of 7200 seconds
        tell application "Microsoft Outlook"
            repeat with f in mail folders
                try
                    set msgCount to count of messages of f
                on error
                    set msgCount to 0
                end try
                if msgCount > 0 then
                    try
                        set firstMsg to message 1 of f
                        set acctEmail to email address of (account of firstMsg)
                        if acctEmail is mailboxEmail then
                            try
                                set fname to name of f as string
                            on error
                                set fname to ""
                            end try
                            if fname is not "" then
                                set entry to "{\"name\":\"" & my jsonEscape(fname) & ¬
                                    "\",\"message_count\":" & msgCount & "}"
                                set end of collected to entry
                            end if
                        end if
                    end try
                end if
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
