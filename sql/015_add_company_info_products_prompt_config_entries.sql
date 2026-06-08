-- Run in Supabase SQL Editor
-- Adds the prompt-assembly building blocks for the Information Agent's
-- Company Info (Contact Details / Links & References) and Products & Services
-- sections to app_config, so they are loaded at runtime instead of being
-- hardcoded. These are injected into the final prompt only when the
-- corresponding section is enabled on the information_bot row.

INSERT INTO public.app_config (key, value)
VALUES (
  'links_references_prompt',
  'Relevant Links & References: If your response relates to any topic below, you MUST include the relevant link(s). Format: mention the link name as the purpose, then the URL.
If the query matches any context above, include a line like: ''For more information about [purpose/name], visit: [URL]'' or ''You can find more details at [name]: [URL]'''
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.app_config (key, value)
VALUES (
  'contact_info_prompt',
  'CONTACT INFORMATION
If the user asks how to contact the business, requests support, wants to schedule a consultation, or needs assistance from a person, include the relevant contact details.
Format: For assistance, contact: Phone: [Phone Number] Email: [Email Address] Address: [Business Address]'
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.app_config (key, value)
VALUES (
  'products_services_prompt',
  'PRODUCTS & SERVICES
If the user asks about products or services, provide only the information relevant to the user''s question.
Available fields: Name, Short Description, Category, Full Description, Price, Discount Price, Status, Images, Rating, Reviews Count, Purchased Count
Do not display all available fields by default. Only return the fields necessary to answer the user''s query. Prioritize concise and relevant information based on user intent.'
)
ON CONFLICT (key) DO NOTHING;
