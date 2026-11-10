# Calendar — quick guide

- **Meetings** have attendees (they receive an invitation with an `.ics` file and Yes / Maybe / No
  links), a location or **room**, and can **repeat** (e.g. `FREQ=WEEKLY;BYDAY=MO;COUNT=10`).
  Cancel or move a single occurrence without touching the series.
- **Rooms** can't be double-booked: an overlapping booking is refused and names the meeting already
  there. Mark a meeting *Free* if it shouldn't block anyone.
- **Private** meetings are visible only to their participants.
- **Scheduling links**: create one (duration, buffer, working hours, notice) and share its link;
  people pick a free slot and the meeting lands in your calendar with an invitation.
- **External calendars**: connect CalDAV (Nextcloud, Fastmail, iCloud …), Google Calendar or
  Microsoft 365 under *External calendars*. Changes on either side, including repeating meetings and
  deletions, are synchronised every 10 minutes (or *Sync now*). Google and Microsoft sign-ins renew
  themselves; if the provider stops accepting them (you removed Tessera's access, changed your password
  or your administrator changed the app), the calendar shows **Needs reconnection**, synchronisation
  pauses and you get a notification: connect the account again and it resumes.
