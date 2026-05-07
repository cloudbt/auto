$urls = @(
    'https://signon.servicenow.com/x_snc_sso_auth.do?pageId=login',
    'https://creativecontentlabtokyo.com/member-menu/',
    'https://teams.microsoft.com/dl/launcher/launcher.html?url=%2F_%23%2Fl%2Fmeetup-join%2F19%3Ameeting_YTM4Y2RiOWEtMmUxMi00ZjY5LTg1NzEtYWVhYjZmMjg3MzIy%40thread.v2%2F0%3Fcontext%3D%257b%2522Tid%2522%253a%2522dbb03f45-1244-4f87-bb71-6338b010567d%2522%252c%2522Oid%2522%253a%252287450d0d-f8d8-4f8d-bfbb-485bb8934ca5%2522%257d%26anon%3Dtrue&type=meetup-join&deeplinkId=1516f29e-63fd-4554-a8b0-bdc546f93733&directDl=true&msLaunch=true&enableMobilePage=true&suppressPrompt=true'
)

foreach ($url in $urls) {
    Start-Process "msedge.exe" $url
}
