import puppeteer from "puppeteer-extra";
import StealthPlugin from "puppeteer-extra-plugin-stealth";
import fs from "fs";

puppeteer.use(StealthPlugin());

const BASE_URL = "https://www.squareyards.com";

async function scrapePDP() {
  console.log("Starting SquareYards PDP Scraper...");

  const browser = await puppeteer.launch({
    headless: false,
    pipe: true,
    defaultViewport: null,
    args: [
      "--start-maximized",
      "--disable-infobars",
      "--no-sandbox",
      "--disable-gpu",
    ],
  });

  const page = await browser.newPage();

  await page.setUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/120 Safari/537.36",
  );

  await page.goto(BASE_URL, { waitUntil: "networkidle2" });
  console.log("SquareYards homepage opened");

  try {
    await page.waitForSelector('[class*="dropdown-input"]', { timeout: 3000 });
    await page.click('[class*="dropdown-input"]');

    await page.waitForSelector("input[type='text']", { timeout: 3000 });
    await page.type("input[type='text']", "Noida", { delay: 100 });

    await page.waitForSelector('[class*="dropdown-menu"]', { timeout: 1000 });
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle2", timeout: 10000 }),
      page.evaluate(() => {
        const items = Array.from(
          document.querySelectorAll('[class*="dropdown-menu"] li'),
        );
        const noidaOption = items.find((el) =>
          el.innerText.toLowerCase().includes("noida"),
        );
        if (noidaOption) noidaOption.click();
      }),
    ]);
    // await page.click('[class*="dropdown-menu"]');
    console.log("Location selected");
  } catch (err) {
    console.log("Location modal not found or already handled.");
  }

  // scroll down
  await page.evaluate(() => {
    window.scrollBy(0, window.innerHeight);
  });

  // Click on Hot Selling Projects in Noida
  try {
    await page.waitForSelector('a[title*="Hot Selling Projects in Noida"]', {
      timeout: 5000,
    });
    await page.click('a[title*="Hot Selling Projects in Noida"]');
    await page.waitForSelector(".listing-card-box", { timeout: 5000 });
    const projectLinks = await page.evaluate(() => {
      const tiles = document.querySelectorAll(".project-card");
      const links = [];
      tiles.forEach((tile) => {
        const linkElement =
          tile.querySelector(".heading-body a.projectDetailUrl")?.href || null;
        if (linkElement) {
          links.push(linkElement);
        }
      });
      return links;
    });

    const results = [];
    for (const link of projectLinks) {
      // ✅ run only for first element
      // if (results.length > 0) break;
      const newPage = await browser.newPage();

      try {
        console.log("Opening project link: ", link);
        await newPage.goto(link, { waitUntil: "domcontentloaded", timeout: 0 });

        // Scroll to load lazy elements
        await newPage.evaluate(() => {
          window.scrollBy(0, window.innerHeight);
        });

        // Try to open amenities modal and extract data
        let amenitiesFromModal = [];
        try {
          await newPage.waitForSelector(".amenities-link-popup-btn");
          await newPage.click(".amenities-link-popup-btn");
          await newPage.waitForSelector(".accordion-item");
          amenitiesFromModal = await newPage.evaluate(() => {
            const result = [];
            const items = document.querySelectorAll(".accordion-item");
            items.forEach((item) => {
              const category = item
                .querySelector(".accordion-header strong")
                ?.innerText.trim();
              const amenities = Array.from(
                item.querySelectorAll(".accordion-body span"),
              )
                .map((el) => el.innerText.trim())
                .filter((text) => text.length > 0);

              if (category && amenities.length > 0) {
                result.push({ category, amenities });
              }
            });
            return result;
          });
          const closeBtn = await newPage.$(".modal-close button");
          if (closeBtn) await closeBtn.click();
        } catch (err) {
          console.log("No modal found or failed to open:", err.message);
        }

        const data = await newPage.evaluate(() => {
          const getText = (selector) =>
            document.querySelector(selector)?.innerText.trim() || null;
          const getAllText = (selector) => {
            return Array.from(document.querySelectorAll(selector))
              .map((el) => el.innerText.trim())
              .filter((text) => text.length > 0);
          };

          const projectTitle = getText("h1")?.split("\n")[0] || null;

          const price = getText(".price-box");

          let projectStatus = null;
          let possession = null;
          let numberOfUnits = null;
          let totalArea = null;

          const statusBlocks = document.querySelectorAll(
            ".unit-status-box .status, .status-box .status",
          );

          statusBlocks.forEach((block) => {
            const label = block.querySelector("span")?.innerText.trim() || "";
            const value =
              block.querySelector("strong")?.innerText.trim() || null;

            if (!value) return;

            if (label.includes("Project Status") && value) {
              projectStatus = value;
            }

            if (label.includes("Possession Starting From") && value) {
              possession = value;
            }

            if (label.includes("Number of Units")) {
              numberOfUnits = value;
            }

            if (label.includes("Total area")) {
              totalArea = value;
            }
          });

          const aboutProject = getAllText("#aboutProject p") || null;

          const BHKType = (() => {
            const units = document.querySelectorAll(".unit");

            for (let unit of units) {
              const label = unit
                .querySelector("span")
                ?.innerText.trim()
                .toLowerCase();

              if (label === "unit config") {
                return (
                  unit.querySelector(".bhk-type")?.innerText.trim() || null
                );
              }
            }

            return null;
          })();

          const size = getText(".unit-value");
          const reraElement = document.querySelector(
            ".accordion-header[data-reraid]",
          );
          const reraId = reraElement
            ? reraElement.getAttribute("data-reraid")
            : null;
          const builderDescElement = document.querySelector(
            "#aboutBuilder .content-box p",
          );
          const builderDescription = builderDescElement
            ? builderDescElement.innerText.trim()
            : null;

          const amenityElements = document.querySelectorAll(
            "#amenities .amenities-list-box ul li span",
          );
          const amenities =
            Array.from(amenityElements).map((el) => el.innerText.trim()) ||
            null;

          const priceList = Array.from(
            document.querySelectorAll("#priceList tbody tr"),
          )
            .map((row) => {
              // BHK Type
              const bhkType =
                row.querySelector("td span")?.innerText.trim() || null;

              // Size (sqft from data attribute)
              const size =
                row.querySelector(".unit-value")?.getAttribute("data-sqft") ||
                row.querySelector(".unit-value")?.innerText.trim() ||
                null;

              // Price
              const price =
                row.querySelector("td:nth-child(2) strong")?.innerText.trim() ||
                null;

              return {
                bhkType,
                size,
                price,
              };
            })
            .filter((item) => item.bhkType || item.size || item.price);

          const recentUpdates = Array.from(
            document.querySelectorAll(
              "#recentUpdates .recent-updates-box article .details p",
            ),
          )
            .map((el) => el.innerText.trim())
            .filter((text) => text.length > 0);

          const landmarksSection = document.querySelector("#mapLandmarks");

          let landmarks = null;

          if (landmarksSection) {
            landmarks = {};

            const boxes =
              landmarksSection.querySelectorAll(".near-distance-box");

            boxes.forEach((box) => {
              const type = box.getAttribute("data-attribute")?.toLowerCase();

              if (!type) return;

              const items = Array.from(box.querySelectorAll("tbody tr"))
                .map((row) => {
                  const name =
                    row.querySelector(".distance-title")?.innerText.trim() ||
                    null;

                  const distance =
                    row.querySelector(".distance span")?.innerText.trim() ||
                    null;

                  if (name && distance) {
                    return { name, distance };
                  }

                  return null;
                })
                .filter(Boolean);

              if (items.length > 0) {
                landmarks[type] = items;
              }
            });
          }

          // Specifications
          const specificationSection =
            document.querySelector("#specifications");

          let specifications = null;

          if (specificationSection) {
            specifications = Array.from(
              specificationSection.querySelectorAll(
                ".specification-table tbody tr",
              ),
            )
              .map((row) => {
                const heading =
                  row
                    .querySelector(".specification-heading")
                    ?.innerText.trim() || null;

                const value =
                  row.querySelector(".specification-value")?.innerText.trim() ||
                  null;

                if (heading && value) {
                  return { heading, value };
                }

                return null;
              })
              .filter(Boolean);
          }

          return {
            projectTitle,
            price,
            projectStatus,
            possession,
            numberOfUnits,
            totalArea,
            aboutProject,
            BHKType,
            size,
            reraId,
            builderDescription,
            amenities,
            priceList,
            recentUpdates,
            // masterPlan,
            landmarks,
            specifications,
          };
        });
        const finalAmenities =
          amenitiesFromModal && amenitiesFromModal.length > 0
            ? amenitiesFromModal
            : data.amenities;

        // Images except Apartment Interiors images
        let imageUrls = [];
        let masterPlanData = null;
        try {
          // Scroll again to ensure gallery loads
          await newPage.evaluate(() => {
            window.scrollBy(0, window.innerHeight);
          });
          const countTag = await newPage.$(".count-tag");

          if (countTag) {
            console.log("Opening gallery...");

            const [newTab] = await Promise.all([
              browser
                .waitForTarget((target) => target.opener() === newPage.target())
                .then((target) => target.page())
                .catch(() => null),

              newPage.evaluate(() => {
                document.querySelector(".count-tag")?.click();
              }),
            ]);

            const galleryPage = newTab || newPage;

            await galleryPage.waitForFunction(
              () => {
                return (
                  document.querySelectorAll(".gallery-left-box a").length > 0
                );
              },
              { timeout: 1000 },
            );
            await new Promise((resolve) => setTimeout(resolve, 2000));

            try {
              await galleryPage.evaluate(() => {
                const masterTab = Array.from(
                  document.querySelectorAll(".gallery-tab-list li"),
                ).find((el) =>
                  el.innerText.trim().toLowerCase().includes("master plan"),
                );

                masterTab?.click();
              });

              await new Promise((resolve) => setTimeout(resolve, 1000));

              masterPlanData = await galleryPage.evaluate(() => {
                const section = document.querySelector(
                  ".sy-gallery.gallery-tab-content[data-tab='Master Plan']",
                );

                if (!section) return null;

                const image = section.querySelector("a")?.href || null;

                const content = Array.from(
                  section.querySelectorAll("p, strong, li"),
                )
                  .map((el) => {
                    const text = el.innerText.trim();
                    if (!text) return null;

                    if (el.tagName === "STRONG") return `\n${text}`;
                    if (el.tagName === "LI") return `• ${text}`;
                    return text;
                  })
                  .filter(Boolean)
                  .join("\n");

                return {
                  image,
                  content,
                };
              });
            } catch (err) {
              console.log("Failed to fetch master plan:", err.message);
            }

            imageUrls = await galleryPage.evaluate(() => {
              const sections = Array.from(
                document.querySelectorAll(".white-box"),
              );

              return sections
                .filter((section) => {
                  const heading = section.querySelector(".white-box-heading");
                  return (
                    heading &&
                    heading.innerText.trim().toLowerCase() !==
                      "apartment interiors"
                  );
                })
                .flatMap((section) =>
                  Array.from(section.querySelectorAll("a"))
                    .map((a) => a.href)
                    .filter(
                      (url) =>
                        url &&
                        url.includes("static.squareyards.com") && // ensures it's an image CDN
                        !url.includes(".mp4"), // avoid videos
                    ),
                );
            });
          } else {
            console.log("Gallery not found for this project.");
          }
        } catch (err) {
          console.log("Gallery not found or failed to load: ", err.message);
        }
        data.images = [...new Set(imageUrls)];
        data.masterPlan = masterPlanData;

        let flat = {};
        try {
          flat = await newPage.evaluate(() => {
            const floorPlans = {};

            const items = Array.from(
              document.querySelectorAll("#lineTabBoxSlider li[data-cat]"),
            );

            items.forEach((item) => {
              const bhkType = item
                .getAttribute("data-cat")
                ?.trim()
                .toUpperCase();

              if (!bhkType) return;

              const size = item.querySelector("span")?.innerText.trim() || null;

              const price =
                item.querySelector("strong")?.innerText.trim() || null;

              const image = item.getAttribute("data-img")?.trim() || null;

              const plan = {
                size,
                price,
                image,
              };

              if (!floorPlans[bhkType]) {
                floorPlans[bhkType] = [];
              }

              floorPlans[bhkType].push(plan);
            });

            return floorPlans;
          });
        } catch (err) {
          console.log("Failed to fetch floor plans:", err.message);
        }

        // Interior images only
        let imageIntUrls = [];
        try {
          // Scroll again to ensure gallery loads
          await newPage.evaluate(() => {
            window.scrollBy(0, window.innerHeight);
          });
          const countTag = await newPage.$(".count-tag");

          if (countTag) {
            console.log("Opening gallery...");

            const [newTab] = await Promise.all([
              browser
                .waitForTarget((target) => target.opener() === newPage.target())
                .then((target) => target.page())
                .catch(() => null),

              newPage.evaluate(() => {
                document.querySelector(".count-tag")?.click();
              }),
            ]);

            const galleryPage = newTab || newPage;

            await galleryPage.waitForFunction(
              () => {
                return (
                  document.querySelectorAll(".gallery-left-box a").length > 0
                );
              },
              { timeout: 1000 },
            );
            await new Promise((resolve) => setTimeout(resolve, 2000));

            imageIntUrls = await galleryPage.evaluate(() => {
              const sections = Array.from(
                document.querySelectorAll(".white-box"),
              );

              return sections
                .filter((section) => {
                  const heading = section.querySelector(".white-box-heading");

                  return (
                    heading &&
                    heading.innerText.trim().toLowerCase() ===
                      "apartment interiors"
                  );
                })
                .flatMap((section) =>
                  Array.from(section.querySelectorAll("a"))
                    .map((a) => a.href)
                    .filter(Boolean),
                );
            });
          } else {
            console.log("Gallery not found for this project.");
          }
        } catch (err) {
          console.log(
            "Gallery not found or failed to load Interior Images: ",
            err.message,
          );
        }
        data.interiorImages = [...new Set(imageIntUrls)];

        results.push({ ...data, amenities: finalAmenities, flats: flat });
      } catch (err) {
        console.log(`Error processing ${link}: ${err.message}`);
        continue;
      }
      await newPage.close();
    }
    fs.writeFileSync(
      "noidaResidentialProPDP.json",
      JSON.stringify(results, null, 2),
      "utf-8",
    );
    console.log("Data saved to noidaResidentialProPDP.json");
  } catch (err) {
    console.log("Not able to fetch Noida Residential Projects");
  }

  // fetching commericial projects in noida
  try {
    await page.waitForSelector('.filterPropertyList li[value*="2"]', {
      timeout: 6000,
    });
    await page.click('.filterPropertyList li[value*="2"]');
    await page.waitForSelector(".listing-card-box", { timeout: 3000 });
    const projectLinks = await page.evaluate(() => {
      const tiles = document.querySelectorAll(".project-card");
      const links = [];
      tiles.forEach((tile) => {
        const linkElement =
          tile.querySelector(".heading-body a.projectDetailUrl")?.href || null;
        if (linkElement) {
          links.push(linkElement);
        }
      });
      return links;
    });

    const results = [];
    for (const link of projectLinks) {
      // ✅ run only for first element
      // if (results.length > 0) break;
      const newPage = await browser.newPage();

      try {
        console.log("Opening project link: ", link);
        await newPage.goto(link, { waitUntil: "domcontentloaded", timeout: 0 });

        // Scroll to load lazy elements
        await newPage.evaluate(() => {
          window.scrollBy(0, window.innerHeight);
        });

        // Try to open amenities modal and extract data
        let amenitiesFromModal = [];
        try {
          await newPage.waitForSelector(".amenities-link-popup-btn");
          await newPage.click(".amenities-link-popup-btn");
          await newPage.waitForSelector(".accordion-item");
          amenitiesFromModal = await newPage.evaluate(() => {
            const result = [];
            const items = document.querySelectorAll(".accordion-item");
            items.forEach((item) => {
              const category = item
                .querySelector(".accordion-header strong")
                ?.innerText.trim();
              const amenities = Array.from(
                item.querySelectorAll(".accordion-body span"),
              )
                .map((el) => el.innerText.trim())
                .filter((text) => text.length > 0);

              if (category && amenities.length > 0) {
                result.push({ category, amenities });
              }
            });
            return result;
          });
          const closeBtn = await newPage.$(".modal-close button");
          if (closeBtn) await closeBtn.click();
        } catch (err) {
          console.log("No modal found or failed to open:", err.message);
        }

        const data = await newPage.evaluate(() => {
          const getText = (selector) =>
            document.querySelector(selector)?.innerText.trim() || null;
          const getAllText = (selector) => {
            return Array.from(document.querySelectorAll(selector))
              .map((el) => el.innerText.trim())
              .filter((text) => text.length > 0);
          };

          const projectTitle = getText("h1")?.split("\n")[0] || null;

          const price = getText(".price-box");

          let projectStatus = null;
          let possession = null;
          let numberOfUnits = null;
          let totalArea = null;

          const statusBlocks = document.querySelectorAll(
            ".unit-status-box .status, .status-box .status",
          );

          statusBlocks.forEach((block) => {
            const label = block.querySelector("span")?.innerText.trim() || "";
            const value =
              block.querySelector("strong")?.innerText.trim() || null;

            if (!value) return;

            if (label.includes("Project Status") && value) {
              projectStatus = value;
            }

            if (label.includes("Possession Starting From") && value) {
              possession = value;
            }

            if (label.includes("Number of Units")) {
              numberOfUnits = value;
            }

            if (label.includes("Total area")) {
              totalArea = value;
            }
          });

          const aboutProject = getAllText("#aboutProject p") || null;

          const BHKType = (() => {
            const units = document.querySelectorAll(".unit");

            for (let unit of units) {
              const label = unit
                .querySelector("span")
                ?.innerText.trim()
                .toLowerCase();

              if (label === "unit config") {
                return (
                  unit.querySelector(".bhk-type")?.innerText.trim() || null
                );
              }
            }

            return null;
          })();

          const size = getText(".unit-value");
          const reraElement = document.querySelector(
            ".accordion-header[data-reraid]",
          );
          const reraId = reraElement
            ? reraElement.getAttribute("data-reraid")
            : null;
          const builderDescElement = document.querySelector(
            "#aboutBuilder .content-box p",
          );
          const builderDescription = builderDescElement
            ? builderDescElement.innerText.trim()
            : null;

          const amenityElements = document.querySelectorAll(
            "#amenities .amenities-list-box ul li span",
          );
          const amenities =
            Array.from(amenityElements).map((el) => el.innerText.trim()) ||
            null;

          const priceList = Array.from(
            document.querySelectorAll("#priceList tbody tr"),
          )
            .map((row) => {
              // BHK Type
              const bhkType =
                row.querySelector("td span")?.innerText.trim() || null;

              // Size (sqft from data attribute)
              const size =
                row.querySelector(".unit-value")?.getAttribute("data-sqft") ||
                row.querySelector(".unit-value")?.innerText.trim() ||
                null;

              // Price
              const price =
                row.querySelector("td:nth-child(2) strong")?.innerText.trim() ||
                null;

              return {
                bhkType,
                size,
                price,
              };
            })
            .filter((item) => item.bhkType || item.size || item.price);

          const recentUpdates = Array.from(
            document.querySelectorAll(
              "#recentUpdates .recent-updates-box article .details p",
            ),
          )
            .map((el) => el.innerText.trim())
            .filter((text) => text.length > 0);

          // // Master Plan (image + content)
          // const masterPlanSection = document.querySelector(
          //   ".commonGalleryContainer .gallery-body .gallery-left-box .sy-gallery.gallery-tab-content[data-tab='Master Plan']",
          // );

          // let masterPlan = null;

          // if (masterPlanSection) {
          //   // ✅ Image
          //   const image =
          //     masterPlanSection.querySelector("a")?.getAttribute("href") ||
          //     null;

          //   // ✅ Flow content (preserve order)
          //   const content = [];

          //   const elements =
          //     masterPlanSection.querySelectorAll("p, strong, li");

          //   elements.forEach((el) => {
          //     const text = el.innerText.trim();
          //     if (!text) return;

          //     if (el.tagName === "STRONG") {
          //       content.push(`\n${text}`); // heading
          //     } else if (el.tagName === "LI") {
          //       content.push(`• ${text}`); // bullet
          //     } else {
          //       content.push(text); // paragraph
          //     }
          //   });

          //   masterPlan = {
          //     image,
          //     content: content.join("\n"),
          //   };
          // }

          // Landmarks
          const landmarksSection = document.querySelector("#mapLandmarks");

          let landmarks = null;

          if (landmarksSection) {
            landmarks = {};

            const boxes =
              landmarksSection.querySelectorAll(".near-distance-box");

            boxes.forEach((box) => {
              const type = box.getAttribute("data-attribute")?.toLowerCase();

              if (!type) return;

              const items = Array.from(box.querySelectorAll("tbody tr"))
                .map((row) => {
                  const name =
                    row.querySelector(".distance-title")?.innerText.trim() ||
                    null;

                  const distance =
                    row.querySelector(".distance span")?.innerText.trim() ||
                    null;

                  if (name && distance) {
                    return { name, distance };
                  }

                  return null;
                })
                .filter(Boolean);

              if (items.length > 0) {
                landmarks[type] = items;
              }
            });
          }

          // Specifications
          const specificationSection =
            document.querySelector("#specifications");

          let specifications = null;

          if (specificationSection) {
            specifications = Array.from(
              specificationSection.querySelectorAll(
                ".specification-table tbody tr",
              ),
            )
              .map((row) => {
                const heading =
                  row
                    .querySelector(".specification-heading")
                    ?.innerText.trim() || null;

                const value =
                  row.querySelector(".specification-value")?.innerText.trim() ||
                  null;

                if (heading && value) {
                  return { heading, value };
                }

                return null;
              })
              .filter(Boolean);
          }

          return {
            projectTitle,
            price,
            projectStatus,
            possession,
            numberOfUnits,
            totalArea,
            aboutProject,
            BHKType,
            size,
            reraId,
            builderDescription,
            amenities,
            priceList,
            recentUpdates,
            // masterPlan,
            landmarks,
            specifications,
          };
        });
        const finalAmenities =
          amenitiesFromModal && amenitiesFromModal.length > 0
            ? amenitiesFromModal
            : data.amenities;

        // Images except Apartment Interiors images
        let imageUrls = [];
        let masterPlanData = null;
        try {
          // Scroll again to ensure gallery loads
          await newPage.evaluate(() => {
            window.scrollBy(0, window.innerHeight);
          });
          const countTag = await newPage.$(".count-tag");

          if (countTag) {
            console.log("Opening gallery...");

            const [newTab] = await Promise.all([
              browser
                .waitForTarget((target) => target.opener() === newPage.target())
                .then((target) => target.page())
                .catch(() => null),

              newPage.evaluate(() => {
                document.querySelector(".count-tag")?.click();
              }),
            ]);

            const galleryPage = newTab || newPage;

            await galleryPage.waitForFunction(
              () => {
                return (
                  document.querySelectorAll(".gallery-left-box a").length > 0
                );
              },
              { timeout: 1000 },
            );
            await new Promise((resolve) => setTimeout(resolve, 2000));

            try {
              await galleryPage.evaluate(() => {
                const masterTab = Array.from(
                  document.querySelectorAll(".gallery-tab-list li"),
                ).find((el) =>
                  el.innerText.trim().toLowerCase().includes("master plan"),
                );

                masterTab?.click();
              });

              await new Promise((resolve) => setTimeout(resolve, 1000));

              masterPlanData = await galleryPage.evaluate(() => {
                const section = document.querySelector(
                  ".sy-gallery.gallery-tab-content[data-tab='Master Plan']",
                );

                if (!section) return null;

                const image = section.querySelector("a")?.href || null;

                const content = Array.from(
                  section.querySelectorAll("p, strong, li"),
                )
                  .map((el) => {
                    const text = el.innerText.trim();
                    if (!text) return null;

                    if (el.tagName === "STRONG") return `\n${text}`;
                    if (el.tagName === "LI") return `• ${text}`;
                    return text;
                  })
                  .filter(Boolean)
                  .join("\n");

                return {
                  image,
                  content,
                };
              });
            } catch (err) {
              console.log("Failed to fetch master plan:", err.message);
            }

            imageUrls = await galleryPage.evaluate(() => {
              const sections = Array.from(
                document.querySelectorAll(".white-box"),
              );

              return sections
                .filter((section) => {
                  const heading = section.querySelector(".white-box-heading");
                  return (
                    heading &&
                    heading.innerText.trim().toLowerCase() !==
                      "apartment interiors"
                  );
                })
                .flatMap((section) =>
                  Array.from(section.querySelectorAll("a"))
                    .map((a) => a.href)
                    .filter(
                      (url) =>
                        url &&
                        url.includes("static.squareyards.com") && // ensures it's an image CDN
                        !url.includes(".mp4"), // avoid videos
                    ),
                );
            });
          } else {
            console.log("Gallery not found for this project.");
          }
        } catch (err) {
          console.log("Gallery not found or failed to load: ", err.message);
        }
        data.images = [...new Set(imageUrls)];
        data.masterPlan = masterPlanData;

        let flat = {};
        try {
          flat = await newPage.evaluate(() => {
            const floorPlans = {};

            const items = Array.from(
              document.querySelectorAll("#lineTabBoxSlider li[data-cat]"),
            );

            items.forEach((item) => {
              const bhkType = item
                .getAttribute("data-cat")
                ?.trim()
                .toUpperCase();

              if (!bhkType) return;

              const size = item.querySelector("span")?.innerText.trim() || null;

              const price =
                item.querySelector("strong")?.innerText.trim() || null;

              const image = item.getAttribute("data-img")?.trim() || null;

              const plan = {
                size,
                price,
                image,
              };

              if (!floorPlans[bhkType]) {
                floorPlans[bhkType] = [];
              }

              floorPlans[bhkType].push(plan);
            });

            return floorPlans;
          });
        } catch (err) {
          console.log("Failed to fetch floor plans:", err.message);
        }

        // Interior images only
        let imageIntUrls = [];
        try {
          // Scroll again to ensure gallery loads
          await newPage.evaluate(() => {
            window.scrollBy(0, window.innerHeight);
          });
          const countTag = await newPage.$(".count-tag");

          if (countTag) {
            console.log("Opening gallery...");

            const [newTab] = await Promise.all([
              browser
                .waitForTarget((target) => target.opener() === newPage.target())
                .then((target) => target.page())
                .catch(() => null),

              newPage.evaluate(() => {
                document.querySelector(".count-tag")?.click();
              }),
            ]);

            const galleryPage = newTab || newPage;

            await galleryPage.waitForFunction(
              () => {
                return (
                  document.querySelectorAll(".gallery-left-box a").length > 0
                );
              },
              { timeout: 1000 },
            );
            await new Promise((resolve) => setTimeout(resolve, 2000));

            imageIntUrls = await galleryPage.evaluate(() => {
              const sections = Array.from(
                document.querySelectorAll(".white-box"),
              );

              return sections
                .filter((section) => {
                  const heading = section.querySelector(".white-box-heading");

                  return (
                    heading &&
                    heading.innerText.trim().toLowerCase() ===
                      "apartment interiors"
                  );
                })
                .flatMap((section) =>
                  Array.from(section.querySelectorAll("a"))
                    .map((a) => a.href)
                    .filter(Boolean),
                );
            });
          } else {
            console.log("Gallery not found for this project.");
          }
        } catch (err) {
          console.log(
            "Gallery not found or failed to load Interior Images: ",
            err.message,
          );
        }
        data.interiorImages = [...new Set(imageIntUrls)];

        results.push({ ...data, amenities: finalAmenities, flats: flat });
      } catch (err) {
        console.log(`Error processing ${link}: ${err.message}`);
        continue;
      }
      await newPage.close();
    }
    fs.writeFileSync(
      "noidaCommercialProPDP.json",
      JSON.stringify(results, null, 2),
      "utf-8",
    );
    console.log("Data saved to noidaCommercialProPDP.json");
  } catch (err) {
    console.log("Not able to fetch Noida Commercial Projects");
  }

  await new Promise((resolve) => setTimeout(resolve, 4000));
  await browser.close();
}
scrapePDP();
