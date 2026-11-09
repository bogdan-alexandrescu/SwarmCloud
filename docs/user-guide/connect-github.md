# Connect GitHub and install the App

Three checklist steps happen here: **Connect GitHub**, **Install the App** and **Enable orgs**.

## Connect GitHub as yourself

1. Open [Config › GitHub access](/config/github), or press **Connect GitHub** on the checklist.
2. On the **Connection** tab, press **Connect GitHub**.
3. GitHub opens. Approve the SwarmCloud App for your account.
4. You come back to the console. The card **GitHub** shows "connected as @your-login".

The sign-in link lasts 10 minutes and works once. If it expired, press **Connect GitHub** again.

## Install the App where your repositories live

Connecting lets the App act as you. Installing it on an account or organisation lets it reach that owner's repositories.

1. In the card **Where the App is installed**, press **Install on your account** for your own repositories.
2. For an organisation, press **Install on an organisation…** and pick it, for example `example-org`.
   If you are not an owner of that organisation, press **Request install**. An owner of `example-org` approves it on GitHub.
3. Back on the checklist, press **Re-check**.
   You should see "installed on …" at **Install the App**.

## Enable your organisations

1. On [Config › GitHub access](/config/github), find the owner in **Where the App is installed**.
2. Press **Enable** next to each owner your agents should reach.
   The checklist step **Enable orgs** is done once at least one is enabled.

## From Claude Code

Type `/sc:setup`. It gives you the GitHub link, waits for you, and then asks which owners to enable.

## Next

[Choose repositories and verify access](choose-repositories.md).
