    # agent = VoiceAgent(
    #     model="grok-voice",
    #     instructions="""
    #     You are a helpful support voice agent.
    #     Keep responses short and conversational.
    #     """,
    #     voice="friendly-support",
    #     tools=[
    #         {
    #             "name": "lookup_user",
    #             "description": "Lookup a user account",
    #         }
    #     ],
    #     event_handler=handle_event,
    #     finish_handler=handle_finish,
    # )

    # agent.connect(
    #     transport="twilio",
    #     call_details={
    #         "call_id": "call_123",
    #         "from": "+14155551234",
    #         "to": "+18005550199",
    #     },
    # )