# Audio Transcription & Translation — Apollo Guide

## Creating a Workload

Step 1: Create a workload

1. Navigate to the "Workload manager" section.
2. Select "Workloads".
3. Click the "NEW WORKLOAD" button.
4. Select "Workspace" from the dropdown menu.

    ![create workload](../fig/transcription_workspace.png)

Step 2: Configure the workload

1. The "Cluster" section will be set automatically; you do not need to change this.
2. Under "Projects", select the project it will be linked to (yours will differ from the examples in the image below depending on your project).

    ![cluster and projects](../fig/transcription_clusterproject.png)

3. Under "Templates", select "transcription-translation-tool". It may not appear on the first page, so use the page numbers at the bottom to navigate through the list.

    ![template](../fig/transcription_template.png)

4. Provide a descriptive name for the workload (e.g. transcription-translation-example).

    ![name](../fig/transcription_name.png)

5. Click the triangle next to "CREATE WORKSPACE", then select "Advanced setup" from the pop-up menu.

    ![advance set up](../fig/transcription_advance_setup.png)

6. Under "Environment", click "Tools".

    ![tools](../fig/transcription_tools.png)

7. Under "Access", the default setting is "All authenticated users". To change this, click the pencil icon.
    
    ![access](../fig/transcription_access.png)

8. In the pop-up menu, select "Specific users and service accounts", then enter your email address in the grey box.

    ![users](../fig/transcription_users.png)

9. To add additional users, click "+ USER OR SERVICE ACCOUNT".

    ![multiple users](../fig/transcription_multiple_users.png)

10. Once you have added all users, click "SAVE".

    ![save](../fig/transcription_save.png)

11. Finally, scroll to the bottom of the page and click "CREATE WORKSPACE".

    ![Create](../fig/transcription_create.png)

12. You will now be taken back to the Run:ai Workloads page.

Step 3: Connect to the Transcription and Translation app

1. Your workload status will start on "Initializing". This may take a couple of minutes.

    ![initializing](../fig/transcription_initalize.png)

2. When the status changes to "Running," select your workload. You can then access the transcription and translation app interface by selecting "Connect."

    ![connect](../fig/transcription_connect.png)

3. The transcription and translation app will now open in a separate tab in your browser.

For instructions on how to use the app, see the [App user guide](transcription_app_guide.md).
